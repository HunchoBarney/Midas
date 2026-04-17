from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from nautilus_trader.model.data import CustomData, DataType, OrderBookDeltas
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientId, InstrumentId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from cbcl_platform.models import (
    KellyCalibration,
    LiveMarketBinding,
    OrderBookLevel,
    OrderBookSnapshot,
    PortfolioSnapshot,
    PriceUpdate,
    StrategyMarketState,
)
from cbcl_platform.nautilus.data import ChainlinkOraclePrice, CoinbaseSpotPrice
from cbcl_platform.nautilus.polymarket_ids import polymarket_instrument_id
from cbcl_platform.nautilus.services import get_runtime_services


class CBCL005NautilusStrategyConfig(StrategyConfig, frozen=True):
    runtime_id: str
    market_refresh_interval_s: int = 15
    state_flush_interval_ms: int = 500


class CBCL005NautilusStrategy(Strategy):
    def __init__(self, config: CBCL005NautilusStrategyConfig) -> None:
        super().__init__(config=config)
        self._services = get_runtime_services(config.runtime_id)
        self._runtime = self._services.runtime
        self._dirty_markets: set[str] = set()
        self._evaluation_scheduled = False
        self._refresh_inflight = False
        self._eval_loop = None
        self._refresh_timer_name = f"{self.id}-MARKET-REFRESH"
        self._current_bindings: dict[str, LiveMarketBinding] = dict(self._services.bindings)
        self._token_to_market: dict[str, str] = {}
        self._instrument_to_market: dict[str, str] = {}
        self._subscribed_instruments: set[str] = set()
        self._pending_instrument_requests: dict[str, int] = {}
        self._last_attempt_ns: dict[str, int] = {}
        self._latest_coinbase: dict[str, PriceUpdate] = dict(self._services.latest_coinbase)
        self._latest_chainlink: dict[str, PriceUpdate] = dict(self._services.latest_chainlink)
        self._sync_binding_indexes()

    def on_start(self) -> None:
        self._eval_loop = asyncio.get_running_loop()
        self._services.mark_feed_connected("polymarket_market", detail="awaiting book sync")
        self.subscribe_data(DataType(CoinbaseSpotPrice), client_id=ClientId("coinbase"))
        self.subscribe_data(DataType(ChainlinkOraclePrice), client_id=ClientId("rtds"))
        self._subscribe_binding_books()
        self._services.set_status("running")
        self._services.write_state(force=True)
        self.clock.set_timer(
            self._refresh_timer_name,
            pd.Timedelta(seconds=self.config.market_refresh_interval_s),
            None,
            None,
            self._on_market_refresh,
            True,
            False,
        )

    def on_stop(self) -> None:
        self.clock.cancel_timer(self._refresh_timer_name)
        self._services.mark_feed_disconnected("polymarket_market", detail="strategy stopped")
        self._services.set_status("stopped")
        self._services.write_state(force=True)

    def on_data(self, data) -> None:  # type: ignore[override]
        payload = data.data if isinstance(data, CustomData) else data
        if isinstance(payload, CoinbaseSpotPrice):
            update = PriceUpdate(
                source="coinbase",
                symbol=payload.coin,
                price=float(payload.price),
                source_event_ts_ns=int(payload.source_event_ts_ns),
                local_receive_ts_ns=int(payload.local_receive_ts_ns),
                volume_24h=float(payload.volume_24h) if payload.volume_24h > 0.0 else None,
            )
            self._latest_coinbase[payload.coin.upper()] = update
            self._services.record_coinbase(update)
            self._mark_coin_dirty(payload.coin.upper())
        elif isinstance(payload, ChainlinkOraclePrice):
            update = PriceUpdate(
                source="chainlink",
                symbol=payload.coin,
                price=float(payload.price),
                source_event_ts_ns=int(payload.source_event_ts_ns),
                local_receive_ts_ns=int(payload.local_receive_ts_ns),
            )
            self._latest_chainlink[payload.coin.upper()] = update
            self._services.record_chainlink(update)
            self._mark_coin_dirty(payload.coin.upper())

    def on_instrument(self, instrument) -> None:  # type: ignore[override]
        instrument_id = str(instrument.id)
        self._pending_instrument_requests.pop(instrument_id, None)
        market_id = self._instrument_to_market.get(instrument_id)
        if market_id is None:
            return
        self._subscribe_binding_books()
        self._mark_market_dirty(market_id)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:  # type: ignore[override]
        instrument_id = str(deltas.instrument_id)
        self._services.record_polymarket_book_event(int(deltas.ts_init))
        market_id = self._instrument_to_market.get(instrument_id)
        if market_id is None:
            return
        binding = self._current_bindings.get(market_id)
        if binding is None:
            return
        self._update_timeline(binding)
        self._mark_market_dirty(market_id)

    def on_order_filled(self, event) -> None:  # type: ignore[override]
        self._services.write_state(force=True)

    def on_order_rejected(self, event) -> None:  # type: ignore[override]
        self._services.write_state(force=True)

    def on_order_denied(self, event) -> None:  # type: ignore[override]
        self._services.write_state(force=True)

    def on_position_opened(self, event) -> None:  # type: ignore[override]
        self._services.write_state(force=True)

    def on_position_closed(self, event) -> None:  # type: ignore[override]
        self._services.write_state(force=True)

    def _on_market_refresh(self, _event) -> None:
        if self._refresh_inflight or self._eval_loop is None:
            return
        self._refresh_inflight = True

        async def _refresh() -> None:
            try:
                bindings = await self._services.registry.refresh()
                self._current_bindings = dict(bindings)
                self._services.set_bindings(bindings)
                self._sync_binding_indexes()
                self._subscribe_binding_books()
                await self._reconcile_resolutions()
                for market_id in self._current_bindings:
                    self._mark_market_dirty(market_id)
            finally:
                self._refresh_inflight = False

        self._eval_loop.create_task(_refresh())

    def _subscribe_binding_books(self) -> None:
        active_instruments: set[str] = set()
        request_retry_ns = self.config.market_refresh_interval_s * 1_000_000_000
        now_ns = self.clock.timestamp_ns()
        for binding in self._current_bindings.values():
            yes_instrument = str(
                polymarket_instrument_id(
                    binding.condition_id,
                    binding.yes_token_id,
                ),
            )
            no_instrument = str(
                polymarket_instrument_id(
                    binding.condition_id,
                    binding.no_token_id,
                ),
            )
            active_instruments.update({yes_instrument, no_instrument})
            self._ensure_instrument_subscription(
                yes_instrument,
                now_ns=now_ns,
                request_retry_ns=request_retry_ns,
            )
            self._ensure_instrument_subscription(
                no_instrument,
                now_ns=now_ns,
                request_retry_ns=request_retry_ns,
            )

        for instrument_id in sorted(self._subscribed_instruments - active_instruments):
            self.unsubscribe_order_book_deltas(InstrumentId.from_str(instrument_id))
            self._subscribed_instruments.discard(instrument_id)
            self._pending_instrument_requests.pop(instrument_id, None)

    def _ensure_instrument_subscription(
        self,
        instrument_id: str,
        *,
        now_ns: int,
        request_retry_ns: int,
    ) -> None:
        instrument = InstrumentId.from_str(instrument_id)
        if self.cache.instrument(instrument) is None:
            last_requested_ns = self._pending_instrument_requests.get(instrument_id)
            if (
                last_requested_ns is None
                or now_ns - last_requested_ns >= request_retry_ns
            ):
                self.request_instrument(instrument)
                self._pending_instrument_requests[instrument_id] = now_ns
            return
        if instrument_id not in self._subscribed_instruments:
            self.subscribe_order_book_deltas(instrument)
            self._subscribed_instruments.add(instrument_id)
            self._pending_instrument_requests.pop(instrument_id, None)

    def _sync_binding_indexes(self) -> None:
        token_to_market: dict[str, str] = {}
        instrument_to_market: dict[str, str] = {}
        for binding in self._current_bindings.values():
            token_to_market[binding.yes_token_id] = binding.market_id
            token_to_market[binding.no_token_id] = binding.market_id
            instrument_to_market[
                str(
                    polymarket_instrument_id(
                        binding.condition_id,
                        binding.yes_token_id,
                    ),
                )
            ] = binding.market_id
            instrument_to_market[
                str(
                    polymarket_instrument_id(
                        binding.condition_id,
                        binding.no_token_id,
                    ),
                )
            ] = binding.market_id
        self._token_to_market = token_to_market
        self._instrument_to_market = instrument_to_market

    def _mark_coin_dirty(self, coin: str) -> None:
        for binding in self._current_bindings.values():
            if binding.coin.upper() == coin.upper():
                self._dirty_markets.add(binding.market_id)
        self._schedule_evaluation()

    def _mark_market_dirty(self, market_id: str) -> None:
        self._dirty_markets.add(market_id)
        self._schedule_evaluation()

    def _schedule_evaluation(self) -> None:
        if self._evaluation_scheduled or self._eval_loop is None:
            return
        self._evaluation_scheduled = True
        self._eval_loop.call_soon(self._drain_dirty_markets)

    def _drain_dirty_markets(self) -> None:
        self._evaluation_scheduled = False
        dirty = sorted(self._dirty_markets)
        self._dirty_markets.clear()
        now_ns = self.clock.timestamp_ns()
        for market_id in dirty:
            self._evaluate_market(market_id, now_ns)
        self._services.write_state()

    def _evaluate_market(self, market_id: str, now_ns: int) -> None:
        binding = self._current_bindings.get(market_id)
        if binding is None:
            return

        yes_book = self._book_snapshot(binding.condition_id, binding.yes_token_id)
        no_book = self._book_snapshot(binding.condition_id, binding.no_token_id)
        coin = binding.coin.upper()
        coinbase = self._latest_coinbase.get(coin)
        chainlink = self._latest_chainlink.get(coin)

        skew_ms = None
        if coinbase is not None and chainlink is not None:
            skew_ms = abs(coinbase.source_event_ts_ns - chainlink.source_event_ts_ns) / 1_000_000.0
            self._services.feed_skew_ms_by_coin[coin] = skew_ms

        state = StrategyMarketState(
            market=binding.to_market_descriptor(),
            coinbase_price=coinbase,
            chainlink_price=chainlink,
            yes_book=yes_book,
            no_book=no_book,
        )

        portfolio = self._portfolio_snapshot(binding)
        calibration = self._calibration()
        decision = self._runtime.strategy.evaluate(
            state,
            portfolio=portfolio,
            calibration=calibration,
            now_ns=now_ns,
        )

        row = self._row_for(binding, decision, state, skew_ms, now_ns)
        self._services.record_opportunity_row(market_id, row)
        if not decision.accepted or decision.intent is None:
            return
        self._services.signals_seen += 1

        if (
            self._runtime.paper_execution
            and self._runtime.paper_execution.portfolio.positions.get(market_id)
        ):
            self._services.signals_blocked_open_position += 1
            row["signal_state"] = "blocked"
            row["reason"] = "open position already active"
            self._services.record_opportunity_row(market_id, row)
            return

        cooldown_ns = self._runtime.config.paper_execution.signal_cooldown_ms * 1_000_000
        last_attempt_ns = self._last_attempt_ns.get(market_id)
        if last_attempt_ns is not None and now_ns - last_attempt_ns < cooldown_ns:
            self._services.signals_blocked_cooldown += 1
            row["signal_state"] = "blocked"
            row["reason"] = "signal cooldown active"
            self._services.record_opportunity_row(market_id, row)
            return

        quote = self._runtime.execution_core.quote_buy(
            yes_book if decision.intent.side.value == "YES" else no_book,
            signal_price=float(decision.intent.signal_price),
            target_shares=float(decision.intent.target_shares),
        )
        row["exec_price"] = round(quote.executable_price, 4)
        row["depth_under_cap"] = round(quote.executable_shares, 4)
        row["signal_state"] = "submitted"
        row["reason"] = "submit"
        self._services.record_opportunity_row(market_id, row)
        self._services.signals_attempted += 1
        self._last_attempt_ns[market_id] = now_ns

        instrument_id = polymarket_instrument_id(binding.condition_id, decision.intent.token_id)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            row["signal_state"] = "blocked"
            row["reason"] = "instrument unavailable in Nautilus cache"
            self._services.record_opportunity_row(market_id, row)
            return

        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=self._order_side(decision.intent.side.value),
            quantity=instrument.make_qty(float(decision.intent.target_shares)),
            price=instrument.make_price(float(min(0.999, max(0.001, quote.executable_price)))),
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(
            order,
            params={"entry_intent": asdict(decision.intent)},
        )

    async def _reconcile_resolutions(self) -> None:
        if self._services.paper_execution is None:
            return
        market_ids = set(self._services.paper_execution.portfolio.positions)
        if not market_ids:
            return
        resolutions = await self._services.registry.resolve_markets(market_ids)
        for binding, winning_token_id in resolutions:
            pnl = self._services.paper_execution.portfolio.settle_market(
                binding.market_id,
                side_from_token(binding, winning_token_id),
            )
            self._services.record_settlement(
                {
                    "market_id": binding.market_id,
                    "winning_token_id": winning_token_id,
                    "pnl_usd": pnl,
                    "resolved_at": datetime.now(UTC).isoformat(),
                }
            )

    def _book_snapshot(self, condition_id: str, token_id: str) -> OrderBookSnapshot | None:
        instrument_id = polymarket_instrument_id(condition_id, token_id)
        book = self.cache.order_book(instrument_id)
        if book is None:
            return None
        asks = tuple(
            OrderBookLevel(price=level.price.as_double(), size=level.size())
            for level in book.asks()
        )
        bids = tuple(
            OrderBookLevel(price=level.price.as_double(), size=level.size())
            for level in book.bids()
        )
        snapshot = OrderBookSnapshot(
            token_id=token_id,
            asks=asks,
            bids=bids,
            source_event_ts_ns=int(book.ts_event),
            local_receive_ts_ns=int(book.ts_last),
        )
        self._services.timeline.add_snapshot(token_id, int(book.ts_last), snapshot)
        return snapshot

    def _update_timeline(self, binding: LiveMarketBinding) -> None:
        yes = self._book_snapshot(binding.condition_id, binding.yes_token_id)
        no = self._book_snapshot(binding.condition_id, binding.no_token_id)
        if yes is not None:
            self._services.timeline.add_snapshot(binding.yes_token_id, yes.local_receive_ts_ns, yes)
        if no is not None:
            self._services.timeline.add_snapshot(binding.no_token_id, no.local_receive_ts_ns, no)

    def _portfolio_snapshot(self, binding: LiveMarketBinding) -> PortfolioSnapshot:
        if self._services.paper_execution is None:
            return PortfolioSnapshot(cash_balance_usd=0.0, total_exposure_usd=0.0, open_positions=0)
        return self._services.paper_execution.portfolio.snapshot()

    def _calibration(self) -> KellyCalibration:
        trade_count = self._services.wins + self._services.losses
        win_rate = self._services.wins / trade_count if trade_count else 0.0
        return KellyCalibration(trade_count=trade_count, win_rate=win_rate)

    def _row_for(
        self,
        binding: LiveMarketBinding,
        decision,
        state: StrategyMarketState,
        skew_ms: float | None,
        now_ns: int,
    ) -> dict[str, Any]:
        coin = binding.coin.upper()
        best_ask = None
        selected_side = None
        if state.yes_book and state.no_book:
            if (
                state.coinbase_price
                and state.chainlink_price
                and state.coinbase_price.price >= state.chainlink_price.price
            ):
                selected_side = "YES"
                best_ask = state.yes_book.best_ask()
            elif state.coinbase_price and state.chainlink_price:
                selected_side = "NO"
                best_ask = state.no_book.best_ask()
        divergence_pct = None
        if state.coinbase_price and state.chainlink_price and state.chainlink_price.price:
            divergence_pct = (
                (state.coinbase_price.price - state.chainlink_price.price)
                / state.chainlink_price.price
            ) * 100.0
        row = {
            "coin": binding.coin,
            "market": f"{binding.coin} {binding.interval.value}",
            "side": selected_side or "awaiting bind",
            "minutes_to_close": round(binding.to_market_descriptor().minutes_to_close(now_ns), 4),
            "divergence_pct": None if divergence_pct is None else round(divergence_pct, 4),
            "best_ask": None if best_ask is None else round(best_ask, 4),
            "exec_price": None,
            "depth_under_cap": None,
            "signal_state": "ready" if decision.accepted else "blocked",
            "reason": decision.reason,
            "event_slug": binding.event_slug,
            "condition_id": binding.condition_id,
        }
        if (
            skew_ms is not None
            and self._runtime.config.freshness.max_feed_skew_ms > 0
            and skew_ms > self._runtime.config.freshness.max_feed_skew_ms
        ):
            self._services.stale_reasons[coin] = f"feed skew gate ({skew_ms:.0f}ms)"
        else:
            current_reason = self._services.stale_reasons.get(coin)
            if isinstance(current_reason, str) and current_reason.startswith("feed skew gate"):
                self._services.stale_reasons.pop(coin, None)
        return row

    @staticmethod
    def _order_side(side: str) -> OrderSide:
        return OrderSide.BUY


def side_from_token(binding: LiveMarketBinding, token_id: str):
    from cbcl_platform.models import OutcomeSide

    return OutcomeSide.YES if token_id == binding.yes_token_id else OutcomeSide.NO
