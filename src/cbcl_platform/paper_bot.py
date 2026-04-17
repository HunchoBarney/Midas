from __future__ import annotations

import math
import random
import signal
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cbcl_platform.constants import clamp_binary_price
from cbcl_platform.models import (
    ContractInterval,
    KellyCalibration,
    MarketDescriptor,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderLifecycle,
    OutcomeSide,
    PriceUpdate,
    RuntimeMode,
    StrategyDecision,
    StrategyMarketState,
)
from cbcl_platform.paper import InMemoryBookTimeline
from cbcl_platform.runtime import TradingRuntime
from cbcl_platform.state_store import RuntimeStateStore


@dataclass
class SimulatedMarket:
    descriptor: MarketDescriptor
    created_at_ns: int
    anchor_price: float
    phase_offset: float
    last_yes_mid: float = 0.5
    last_divergence: float = 0.0
    last_coinbase_price: float = 0.0
    last_chainlink_price: float = 0.0


class PaperTradingBot:
    def __init__(self, *, runtime: TradingRuntime, state_store: RuntimeStateStore) -> None:
        if runtime.mode != RuntimeMode.PAPER or runtime.paper_execution is None:
            raise ValueError("PaperTradingBot requires a paper runtime.")
        self._runtime = runtime
        self._store = state_store
        self._timeline = InMemoryBookTimeline()
        self._rng = random.Random(runtime.config.paper_execution.random_seed)
        self._calibration_wins = 164
        self._calibration_total = 200
        self._markets: dict[str, SimulatedMarket] = {}
        self._market_seq = 0
        self._last_attempt_ns: dict[str, int] = {}
        self._signals_seen = 0
        self._signals_accepted = 0
        self._signals_blocked_open_position = 0
        self._signals_blocked_cooldown = 0
        self._orders_submitted = 0
        self._fill_count = 0
        self._partial_fill_count = 0
        self._reject_count = 0
        self._settlement_count = 0
        self._settlement_wins = 0
        self._settlement_losses = 0
        self._recent_orders: deque[dict[str, Any]] = deque(maxlen=48)
        self._recent_fills: deque[dict[str, Any]] = deque(maxlen=48)
        self._recent_rejects: deque[dict[str, Any]] = deque(maxlen=48)
        self._recent_settlements: deque[dict[str, Any]] = deque(maxlen=48)
        self._last_opportunity_rows: list[dict[str, Any]] = []
        self._started_ns = time.time_ns()
        self._last_flush_ns = 0
        self._tick_count = 0
        self._tick_avg_ms = 0.0
        self._last_tick_ms = 0.0
        self._max_tick_ms = 0.0
        self._running = True

    def install_signal_handlers(self) -> None:
        def _stop(_signum: int, _frame: object) -> None:
            self._running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    def run(self, *, duration_seconds: float = 0.0) -> int:
        self.install_signal_handlers()
        now_ns = time.time_ns()
        self._bootstrap_markets(now_ns)
        self._write_state(status="starting", opportunity_rows=[])
        stop_at_ns = (
            now_ns + int(duration_seconds * 1_000_000_000) if duration_seconds > 0 else None
        )
        while self._running:
            loop_started_ns = time.time_ns()
            self._tick(loop_started_ns)
            tick_ms = (time.time_ns() - loop_started_ns) / 1_000_000.0
            self._record_tick_latency(tick_ms)
            if stop_at_ns is not None and time.time_ns() >= stop_at_ns:
                self._running = False
                break
            sleep_s = max(
                0.01,
                (self._runtime.config.paper_execution.loop_interval_ms / 1000.0)
                - (tick_ms / 1000.0),
            )
            time.sleep(sleep_s)
        self._write_state(status="stopped")
        return 0

    def _bootstrap_markets(self, now_ns: int) -> None:
        for coin in self._runtime.config.markets:
            for interval in (
                ContractInterval.FIVE_MINUTES,
                ContractInterval.FIFTEEN_MINUTES,
            ):
                market = self._new_market(str(coin).upper(), interval, now_ns)
                self._markets[market.descriptor.market_id] = market

    def _new_market(
        self,
        coin: str,
        interval: ContractInterval,
        now_ns: int,
    ) -> SimulatedMarket:
        self._market_seq += 1
        duration_s = (
            self._runtime.config.paper_execution.market_duration_5m_s
            if interval == ContractInterval.FIVE_MINUTES
            else self._runtime.config.paper_execution.market_duration_15m_s
        )
        market_id = f"{coin.lower()}-{interval.value}-{self._market_seq:04d}"
        descriptor = MarketDescriptor(
            market_id=market_id,
            event_slug=f"{coin.lower()}-updown-{interval.value}",
            coin=coin,
            interval=interval,
            expires_at_ns=now_ns + int(duration_s * 1_000_000_000),
            yes_token_id=f"{market_id}-yes",
            no_token_id=f"{market_id}-no",
        )
        return SimulatedMarket(
            descriptor=descriptor,
            created_at_ns=now_ns,
            anchor_price=self._anchor_price_for(coin),
            phase_offset=self._rng.random() * math.tau,
        )

    def _anchor_price_for(self, coin: str) -> float:
        base_prices = {
            "BTC": 100_000.0,
            "ETH": 3_500.0,
            "SOL": 140.0,
            "XRP": 2.0,
        }
        base = base_prices.get(str(coin).upper(), 100.0)
        return float(base * (1.0 + self._rng.uniform(-0.015, 0.015)))

    def _tick(self, now_ns: int) -> None:
        self._tick_count += 1
        rows: list[dict[str, Any]] = []
        for slot_market_id, market in list(self._markets.items()):
            if now_ns >= market.descriptor.expires_at_ns:
                market = self._settle_and_roll_market(slot_market_id, market, now_ns)

            market_state = self._simulate_market_state(market, now_ns)
            decision = self._runtime.strategy.evaluate(
                market_state,
                portfolio=self._runtime.paper_execution.portfolio.snapshot(),
                calibration=self._calibration(),
                now_ns=now_ns,
            )
            rows.append(self._opportunity_row(market, market_state, decision, now_ns))
            if decision.accepted:
                self._signals_seen += 1
                if self._maybe_execute(market, decision, now_ns):
                    self._signals_accepted += 1

        self._last_opportunity_rows = self._sorted_rows(rows)
        if self._should_flush(now_ns):
            self._write_state(status="running", opportunity_rows=self._last_opportunity_rows)

    def _simulate_market_state(
        self,
        market: SimulatedMarket,
        now_ns: int,
    ) -> StrategyMarketState:
        elapsed_s = max(0.0, (now_ns - market.created_at_ns) / 1_000_000_000.0)
        base_trend = 0.0018 * math.sin((elapsed_s / 10.0) + market.phase_offset)
        divergence = (0.0011 * math.sin((elapsed_s / 3.7) + market.phase_offset)) + (
            self._rng.uniform(-0.00010, 0.00010)
        )
        chainlink_price = market.anchor_price * (1.0 + base_trend)
        coinbase_price = chainlink_price * (1.0 + divergence)

        yes_mid = max(
            0.16,
            min(
                0.84,
                0.5 + (divergence * 230.0) + self._rng.uniform(-0.008, 0.008),
            ),
        )
        no_mid = 1.0 - yes_mid

        market.last_divergence = divergence
        market.last_yes_mid = yes_mid
        market.last_coinbase_price = coinbase_price
        market.last_chainlink_price = chainlink_price

        coinbase_update = PriceUpdate(
            source="coinbase",
            symbol=market.descriptor.coin,
            price=round(coinbase_price, 6),
            source_event_ts_ns=now_ns,
            local_receive_ts_ns=now_ns,
        )
        chainlink_update = PriceUpdate(
            source="chainlink",
            symbol=market.descriptor.coin,
            price=round(chainlink_price, 6),
            source_event_ts_ns=now_ns,
            local_receive_ts_ns=now_ns,
        )
        yes_book = self._book_for_mid(market.descriptor.yes_token_id, yes_mid, now_ns)
        no_book = self._book_for_mid(market.descriptor.no_token_id, no_mid, now_ns)
        self._timeline.add_snapshot(market.descriptor.yes_token_id, now_ns, yes_book)
        self._timeline.add_snapshot(market.descriptor.no_token_id, now_ns, no_book)
        return StrategyMarketState(
            market=market.descriptor,
            coinbase_price=coinbase_update,
            chainlink_price=chainlink_update,
            yes_book=yes_book,
            no_book=no_book,
        )

    def _book_for_mid(self, token_id: str, mid: float, now_ns: int) -> OrderBookSnapshot:
        spread = 0.02 + self._rng.random() * 0.02
        best_bid = clamp_binary_price(mid - (spread / 2.0))
        best_ask = clamp_binary_price(mid + (spread / 2.0))
        if best_ask <= best_bid:
            best_ask = clamp_binary_price(best_bid + 0.01)
        if self._rng.random() < 0.3:
            top_size = round(self._rng.uniform(10.0, 18.0), 2)
        else:
            top_size = round(self._rng.uniform(28.0, 42.0), 2)
        top_size = max(self._runtime.config.kelly.min_shares, top_size)
        asks = []
        bids = []
        for idx in range(3):
            ask_price = clamp_binary_price(best_ask + (idx * 0.01))
            bid_price = clamp_binary_price(best_bid - (idx * 0.01))
            asks.append(
                OrderBookLevel(
                    price=ask_price,
                    size=round(top_size * (1.0 + (idx * 0.65)), 2),
                )
            )
            bids.append(
                OrderBookLevel(
                    price=bid_price,
                    size=round(top_size * (1.2 + (idx * 0.35)), 2),
                )
            )
        return OrderBookSnapshot(
            token_id=token_id,
            asks=tuple(asks),
            bids=tuple(bids),
            source_event_ts_ns=now_ns,
            local_receive_ts_ns=now_ns,
        )

    def _maybe_execute(
        self,
        market: SimulatedMarket,
        decision: StrategyDecision,
        now_ns: int,
    ) -> bool:
        if decision.intent is None:
            return False
        market_id = market.descriptor.market_id
        open_positions = self._runtime.paper_execution.portfolio.positions
        if market_id in open_positions:
            self._signals_blocked_open_position += 1
            return False
        cooldown_ns = self._runtime.config.paper_execution.signal_cooldown_ms * 1_000_000
        last_attempt_ns = self._last_attempt_ns.get(market_id, 0)
        if last_attempt_ns and (now_ns - last_attempt_ns) < cooldown_ns:
            self._signals_blocked_cooldown += 1
            return False

        timing = self._runtime.paper_execution.sample_timing(decision.intent.decision_ts_ns)
        self._prime_submit_time_snapshot(market, timing.submit_ts_ns)
        lifecycle = self._runtime.paper_execution.execute_intent(
            decision.intent,
            book_timeline=self._timeline,
            timing=timing,
        )
        self._orders_submitted += 1
        self._last_attempt_ns[market_id] = now_ns
        order_row = self._order_row(market, lifecycle)
        self._recent_orders.appendleft(order_row)
        if lifecycle.status.value in {"filled", "partial"}:
            self._fill_count += 1
            if lifecycle.status.value == "partial":
                self._partial_fill_count += 1
            self._recent_fills.appendleft(order_row)
        else:
            self._reject_count += 1
            self._recent_rejects.appendleft(order_row)
        return True

    def _prime_submit_time_snapshot(self, market: SimulatedMarket, submit_ts_ns: int) -> None:
        if submit_ts_ns <= market.created_at_ns:
            return

        rng_state = self._rng.getstate()
        previous_state = (
            market.last_yes_mid,
            market.last_divergence,
            market.last_coinbase_price,
            market.last_chainlink_price,
        )
        try:
            self._simulate_market_state(market, submit_ts_ns)
        finally:
            self._rng.setstate(rng_state)
            (
                market.last_yes_mid,
                market.last_divergence,
                market.last_coinbase_price,
                market.last_chainlink_price,
            ) = previous_state

    def _settle_and_roll_market(
        self,
        slot_market_id: str,
        market: SimulatedMarket,
        now_ns: int,
    ) -> SimulatedMarket:
        self._last_attempt_ns.pop(market.descriptor.market_id, None)
        winning_side = OutcomeSide.YES if market.last_yes_mid >= 0.5 else OutcomeSide.NO
        pnl = self._runtime.paper_execution.portfolio.settle_market(
            market.descriptor.market_id,
            winning_side,
        )
        if pnl != 0.0:
            self._settlement_count += 1
            if pnl > 0:
                self._settlement_wins += 1
                self._calibration_wins += 1
            else:
                self._settlement_losses += 1
            self._calibration_total += 1
            self._recent_settlements.appendleft(
                {
                    "time": self._time_label(now_ns),
                    "market_id": market.descriptor.market_id,
                    "coin": market.descriptor.coin,
                    "interval": market.descriptor.interval.value,
                    "winning_side": winning_side.value,
                    "pnl_usd": round(pnl, 4),
                }
            )
        replacement = self._new_market(
            market.descriptor.coin,
            market.descriptor.interval,
            now_ns,
        )
        self._markets.pop(slot_market_id, None)
        self._markets[replacement.descriptor.market_id] = replacement
        return replacement

    def _opportunity_row(
        self,
        market: SimulatedMarket,
        state: StrategyMarketState,
        decision: StrategyDecision,
        now_ns: int,
    ) -> dict[str, Any]:
        divergence = market.last_divergence * 100.0
        selected_side = "YES" if market.last_divergence >= 0 else "NO"
        selected_book = state.yes_book if selected_side == "YES" else state.no_book
        best_ask = selected_book.best_ask() if selected_book else None
        exec_price = best_ask
        depth_under_cap = (
            selected_book.shares_available_at_or_below(self._runtime.config.strategy.max_buy_price)
            if selected_book
            else 0.0
        )
        if decision.accepted and decision.intent and selected_book:
            quote = self._runtime.execution_core.quote_buy(
                selected_book,
                signal_price=decision.intent.signal_price,
                target_shares=decision.intent.target_shares,
            )
            exec_price = quote.executable_price
            depth_under_cap = selected_book.shares_available_at_or_below(
                self._runtime.config.strategy.max_buy_price
            )
        freshness_ms = max(
            state.coinbase_price.age_ms(now_ns),
            state.chainlink_price.age_ms(now_ns),
            state.yes_book.age_ms(now_ns),
            state.no_book.age_ms(now_ns),
        )
        return {
            "coin": market.descriptor.coin,
            "market": f"{market.descriptor.coin} {market.descriptor.interval.value}",
            "side": selected_side,
            "minutes_to_close": round(market.descriptor.minutes_to_close(now_ns), 2),
            "divergence_pct": round(divergence, 4),
            "best_ask": round(float(best_ask or 0.0), 4) if best_ask is not None else None,
            "exec_price": round(float(exec_price or 0.0), 4) if exec_price is not None else None,
            "depth_under_cap": round(depth_under_cap, 2),
            "freshness": f"{freshness_ms:.0f}ms",
            "signal_state": "READY" if decision.accepted else decision.reason,
        }

    def _order_row(self, market: SimulatedMarket, lifecycle: OrderLifecycle) -> dict[str, Any]:
        submit_ms = (lifecycle.submit_ts_ns - lifecycle.decision_ts_ns) / 1_000_000.0
        ack_ms = (
            (lifecycle.ack_ts_ns - lifecycle.submit_ts_ns) / 1_000_000.0
            if lifecycle.ack_ts_ns is not None
            else None
        )
        confirm_ms = (
            (lifecycle.confirmed_ts_ns - lifecycle.fill_ts_ns) / 1_000_000.0
            if lifecycle.confirmed_ts_ns is not None and lifecycle.fill_ts_ns is not None
            else None
        )
        return {
            "time": self._time_label(lifecycle.decision_ts_ns),
            "market_id": lifecycle.market_id,
            "market": f"{market.descriptor.coin} {market.descriptor.interval.value}",
            "coin": market.descriptor.coin,
            "interval": market.descriptor.interval.value,
            "side": lifecycle.side.value,
            "status": lifecycle.status.value,
            "reason": lifecycle.reason,
            "limit_price": round(lifecycle.limit_price, 4),
            "requested_shares": round(lifecycle.requested_shares, 2),
            "filled_shares": round(lifecycle.fill.filled_shares, 2),
            "average_price": round(lifecycle.fill.average_price, 4),
            "total_cost": round(lifecycle.fill.total_cost, 4),
            "trade_fee_usd": round(lifecycle.fill.trade_fee_usd, 4),
            "submit_ms": round(submit_ms, 1),
            "ack_ms": round(ack_ms, 1) if ack_ms is not None else None,
            "confirm_ms": round(confirm_ms, 1) if confirm_ms is not None else None,
        }

    def _calibration(self) -> KellyCalibration:
        win_rate = (
            self._calibration_wins / self._calibration_total
            if self._calibration_total > 0
            else 0.82
        )
        return KellyCalibration(
            trade_count=self._calibration_total,
            win_rate=round(win_rate, 6),
        )

    def _portfolio_payload(self) -> dict[str, Any]:
        portfolio = self._runtime.paper_execution.portfolio
        snapshot = portfolio.snapshot()
        positions = []
        for market_id, position in sorted(portfolio.positions.items()):
            positions.append(
                {
                    "market_id": market_id,
                    "yes_shares": round(position.yes_shares, 4),
                    "no_shares": round(position.no_shares, 4),
                    "cost_basis_usd": round(position.cost_basis_usd, 4),
                    "fees_paid_usd": round(position.fees_paid_usd, 4),
                }
            )
        return {
            "cash_balance_usd": round(snapshot.cash_balance_usd, 4),
            "total_exposure_usd": round(snapshot.total_exposure_usd, 4),
            "realized_pnl_usd": round(portfolio.realized_pnl_usd, 4),
            "open_positions": snapshot.open_positions,
            "positions": positions,
            "empty_state": (
                "No positions are open yet. The paper loop is running and will "
                "populate this table when entries fill."
            ),
            "mode_note": (
                "Portfolio is owned by the active paper bot process and updated "
                "through the realistic execution adapter."
            ),
        }

    def _write_state(
        self,
        *,
        status: str,
        opportunity_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        now_ns = time.time_ns()
        payload = {
            "generated_at": self._iso_label(now_ns),
            "status": status,
            "loop": {
                "started_at": self._iso_label(self._started_ns),
                "last_tick_at": self._iso_label(now_ns),
                "tick_count": self._tick_count,
                "last_tick_ms": round(self._last_tick_ms, 3),
                "avg_tick_ms": round(self._tick_avg_ms, 3),
                "max_tick_ms": round(self._max_tick_ms, 3),
            },
            "metrics": {
                "signals_seen": self._signals_seen,
                "signals_accepted": self._signals_accepted,
                "signals_blocked_open_position": self._signals_blocked_open_position,
                "signals_blocked_cooldown": self._signals_blocked_cooldown,
                "orders_submitted": self._orders_submitted,
                "fills": self._fill_count,
                "partial_fills": self._partial_fill_count,
                "rejections": self._reject_count,
                "settlements": self._settlement_count,
                "wins": self._settlement_wins,
                "losses": self._settlement_losses,
                "win_rate_pct": round(
                    (self._settlement_wins / self._settlement_count * 100.0)
                    if self._settlement_count
                    else 0.0,
                    2,
                ),
            },
            "opportunities": {
                "status": "live",
                "summary": "Live simulated opportunity rows from the active paper bot.",
                "columns": [
                    "coin",
                    "market",
                    "side",
                    "minutes_to_close",
                    "divergence_pct",
                    "best_ask",
                    "exec_price",
                    "depth_under_cap",
                    "freshness",
                    "signal_state",
                ],
                "rows": list(opportunity_rows or self._last_opportunity_rows),
            },
            "portfolio": self._portfolio_payload(),
            "execution": {
                "orders": list(self._recent_orders),
                "fills": list(self._recent_fills),
                "rejects": list(self._recent_rejects),
                "settlements": list(self._recent_settlements),
            },
            "system": {
                "feed_mode": "simulated",
                "state_path": str(self._store.path),
                "runtime_mode": self._runtime.mode.value,
                "latency_mode": "submit-time book evaluation with modeled delays",
            },
        }
        self._store.write(payload)
        self._last_flush_ns = now_ns

    def _should_flush(self, now_ns: int) -> bool:
        if self._last_flush_ns == 0:
            return True
        flush_ns = self._runtime.config.paper_execution.state_flush_interval_ms * 1_000_000
        return (now_ns - self._last_flush_ns) >= flush_ns

    def _record_tick_latency(self, tick_ms: float) -> None:
        self._last_tick_ms = tick_ms
        self._max_tick_ms = max(self._max_tick_ms, tick_ms)
        if self._tick_count == 1:
            self._tick_avg_ms = tick_ms
            return
        self._tick_avg_ms += (tick_ms - self._tick_avg_ms) / max(1, self._tick_count)

    @staticmethod
    def _sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def _score(row: dict[str, Any]) -> tuple[int, float]:
            ready = 0 if str(row.get("signal_state")) == "READY" else 1
            div = abs(float(row.get("divergence_pct") or 0.0))
            return (ready, -div)

        return sorted(rows, key=_score)

    @staticmethod
    def _iso_label(ts_ns: int) -> str:
        return datetime.fromtimestamp(ts_ns / 1_000_000_000.0, tz=UTC).isoformat()

    @staticmethod
    def _time_label(ts_ns: int) -> str:
        return datetime.fromtimestamp(ts_ns / 1_000_000_000.0, tz=UTC).strftime("%H:%M:%S")
