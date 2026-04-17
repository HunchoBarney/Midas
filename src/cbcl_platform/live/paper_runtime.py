from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from cbcl_platform.live.coinbase_data_client import CoinbaseDataClient
from cbcl_platform.live.health_service import HealthService
from cbcl_platform.live.market_registry import MarketRegistry
from cbcl_platform.live.nautilus_node import NautilusSupportContext, build_nautilus_support_context
from cbcl_platform.live.order_tracker import OrderTracker
from cbcl_platform.live.polymarket_books import PolymarketBookService
from cbcl_platform.live.polymarket_paper_exec_client import PolymarketPaperExecClient
from cbcl_platform.live.recorder import Recorder
from cbcl_platform.live.rtds_data_client import RtdsDataClient
from cbcl_platform.live.settlement_service import SettlementService
from cbcl_platform.models import (
    KellyCalibration,
    LiveMarketBinding,
    MarketResolution,
    RuntimeMode,
    StrategyDecision,
    StrategyMarketState,
)
from cbcl_platform.paper import InMemoryBookTimeline
from cbcl_platform.runtime import TradingRuntime
from cbcl_platform.state_store import RuntimeStateStore


class RealFeedPaperRuntime:
    def __init__(self, *, runtime: TradingRuntime, state_store: RuntimeStateStore) -> None:
        if runtime.mode != RuntimeMode.PAPER or runtime.paper_execution is None:
            raise ValueError("RealFeedPaperRuntime requires a paper TradingRuntime")
        self._runtime = runtime
        self._store = state_store
        self._timeline = InMemoryBookTimeline()
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8_192)
        self._health = HealthService(freshness=runtime.config.freshness)
        self._coinbase = CoinbaseDataClient(config=runtime.config.coinbase_ws, emit=self._emit)
        self._rtds = RtdsDataClient(config=runtime.config.rtds_ws, emit=self._emit)
        self._recorder = Recorder(runtime.config.recorder)
        self._tracker = OrderTracker()
        self._paper_exec = PolymarketPaperExecClient(runtime.paper_execution)
        self._settlement = SettlementService(portfolio=runtime.paper_execution.portfolio)
        self._ctx: NautilusSupportContext | None = None
        self._registry: MarketRegistry | None = None
        self._books: PolymarketBookService | None = None
        self._running = True
        self._latest_coinbase: dict[str, Any] = {}
        self._latest_chainlink: dict[str, Any] = {}
        self._markets: dict[str, LiveMarketBinding] = {}
        self._known_markets: dict[str, LiveMarketBinding] = {}
        self._last_rows: dict[str, dict[str, Any]] = {}
        self._last_attempt_ns: dict[str, int] = {}
        self._signals_seen = 0
        self._signals_attempted = 0
        self._signals_blocked_open_position = 0
        self._signals_blocked_cooldown = 0
        self._settlements = 0
        self._wins = 0
        self._losses = 0
        self._started_ns = time.time_ns()
        self._last_flush_ns = 0
        self._tick_count = 0
        self._tick_avg_ms = 0.0
        self._max_tick_ms = 0.0
        self._last_tick_ms = 0.0

    async def run(self, *, duration_seconds: float = 0.0) -> int:
        self._ensure_services()
        await self._recorder.start()
        self._write_state(status="starting")
        end_ns = (
            time.time_ns() + int(duration_seconds * 1_000_000_000) if duration_seconds > 0 else None
        )
        await self._bootstrap()
        tasks = [
            asyncio.create_task(self._registry.run(self._emit), name="registry"),
            asyncio.create_task(self._coinbase.run(), name="coinbase"),
            asyncio.create_task(self._rtds.run(), name="rtds"),
        ]
        try:
            while self._running:
                tick_started_ns = time.time_ns()
                timeout = 0.05
                if end_ns is not None:
                    remaining = (end_ns - tick_started_ns) / 1_000_000_000
                    if remaining <= 0:
                        break
                    timeout = max(0.01, min(timeout, remaining))
                try:
                    event = await asyncio.wait_for(self._events.get(), timeout=timeout)
                    await self._handle_event(event)
                except TimeoutError:
                    pass
                await self._process_due_orders()
                self._write_state(status="running")
                tick_ms = (time.time_ns() - tick_started_ns) / 1_000_000.0
                self._record_tick_latency(tick_ms)
        finally:
            self._running = False
            self._registry.stop()
            self._coinbase.stop()
            self._rtds.stop()
            for task in tasks:
                task.cancel()
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._books.stop(), timeout=2.0)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._recorder.stop(), timeout=2.0)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._ctx.http_client.aclose(), timeout=2.0)
            self._write_state(status="stopped")
        return 0

    async def _bootstrap(self) -> None:
        assert self._registry is not None
        assert self._books is not None
        self._markets = await self._registry.bootstrap()
        self._known_markets.update(self._markets)
        await self._books.sync_tokens(self._all_token_ids())

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        with suppress(asyncio.QueueEmpty):
            self._events.get_nowait()
        with suppress(asyncio.QueueFull):
            self._events.put_nowait(event)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        assert self._registry is not None
        event_type = event["type"]
        if event_type == "market_registry":
            self._markets = dict(event["markets"])
            self._known_markets.update(self._markets)
            await self._books.sync_tokens(self._all_token_ids())
            for market_id in self._markets:
                self._evaluate_market(market_id, time.time_ns())
            self._recorder.emit(
                "market_registry",
                {"ts_ns": time.time_ns(), "markets": sorted(self._markets)},
            )
            return
        if event_type == "book_snapshot":
            token_id = str(event["token_id"])
            binding = self._registry.market_for_token(token_id)
            if binding:
                self._evaluate_market(binding.market_id, time.time_ns())
            return
        if event_type == "coinbase_price":
            coin = str(event["coin"])
            self._latest_coinbase[coin] = event["update"]
            self._health.mark_connected("coinbase")
            self._health.mark_event("coinbase", ts_ns=event["update"].local_receive_ts_ns)
            self._recorder.emit(
                "coinbase_price",
                {
                    "ts_ns": event["update"].local_receive_ts_ns,
                    "coin": coin,
                    "price": event["update"].price,
                },
            )
            self._evaluate_coin(coin, time.time_ns())
            return
        if event_type == "chainlink_price":
            coin = str(event["coin"])
            self._latest_chainlink[coin] = event["update"]
            self._health.mark_connected("chainlink")
            self._health.mark_event("chainlink", ts_ns=event["update"].local_receive_ts_ns)
            self._recorder.emit(
                "chainlink_price",
                {
                    "ts_ns": event["update"].local_receive_ts_ns,
                    "coin": coin,
                    "price": event["update"].price,
                },
            )
            self._evaluate_coin(coin, time.time_ns())
            return
        if event_type == "feed_status":
            feed = str(event["feed"])
            if event.get("connected"):
                self._health.mark_connected(feed, detail=str(event.get("detail") or ""))
            else:
                self._health.mark_disconnected(feed, detail=str(event.get("detail") or ""))
            return
        if event_type == "feed_event":
            self._health.mark_event(
                str(event["feed"]),
                ts_ns=int(event.get("ts_ns") or time.time_ns()),
            )
            return
        if event_type == "resolution_hint":
            await self._reconcile_resolutions()

    async def _process_due_orders(self) -> None:
        assert self._books is not None
        now_ns = time.time_ns()
        for lifecycle in self._paper_exec.process_due(
            now_ns=now_ns,
            book_timeline=self._timeline,
            matching_engine_blocked=False,
        ):
            self._tracker.record_lifecycle(lifecycle)
            self._recorder.emit(
                "paper_order_executed",
                {
                    "ts_ns": lifecycle.submit_ts_ns,
                    "market_id": lifecycle.market_id,
                    "status": lifecycle.status.value,
                    "reason": lifecycle.reason,
                    "filled_shares": lifecycle.fill.filled_shares,
                    "average_price": lifecycle.fill.average_price,
                },
            )
        await self._reconcile_resolutions()

    async def _reconcile_resolutions(self) -> None:
        assert self._registry is not None
        assert self._books is not None
        market_ids = set(self._runtime.paper_execution.portfolio.positions)
        market_ids.update(self._books.resolution_hints())
        if not market_ids:
            return
        resolutions = await self._registry.resolve_markets(market_ids)
        for binding, winning_token_id in resolutions:
            self._known_markets[binding.market_id] = binding
            resolution = MarketResolution(
                market_id=binding.market_id,
                winning_token_id=winning_token_id,
                resolved_ts_ns=time.time_ns(),
                source="gamma_reconcile",
            )
            settlement = self._settlement.settle(binding, resolution)
            self._books.clear_resolution_hint(binding.market_id)
            if settlement is None:
                continue
            self._settlements += 1
            if float(settlement["pnl_usd"]) >= 0.0:
                self._wins += 1
            else:
                self._losses += 1
            self._tracker.record_settlement(settlement)
            self._recorder.emit(
                "position_settled",
                {
                    "ts_ns": resolution.resolved_ts_ns,
                    "market_id": binding.market_id,
                    "winning_token_id": winning_token_id,
                    "pnl_usd": settlement["pnl_usd"],
                },
            )

    def _evaluate_coin(self, coin: str, now_ns: int) -> None:
        for binding in self._markets.values():
            if binding.coin == coin:
                self._evaluate_market(binding.market_id, now_ns)

    def _evaluate_market(self, market_id: str, now_ns: int) -> None:
        assert self._books is not None
        binding = self._markets.get(market_id)
        if binding is None:
            return
        yes_book = self._books.snapshot(binding.yes_token_id)
        no_book = self._books.snapshot(binding.no_token_id)
        state = StrategyMarketState(
            market=binding.to_market_descriptor(),
            coinbase_price=self._latest_coinbase.get(binding.coin),
            chainlink_price=self._latest_chainlink.get(binding.coin),
            yes_book=yes_book,
            no_book=no_book,
        )
        skew_ms = self._feed_skew_ms(binding.coin)
        decision = self._runtime.strategy.evaluate(
            state,
            portfolio=self._runtime.paper_execution.portfolio.snapshot(),
            calibration=self._calibration(),
            now_ns=now_ns,
        )
        if (
            self._runtime.config.freshness.max_feed_skew_ms > 0
            and skew_ms is not None
            and skew_ms > self._runtime.config.freshness.max_feed_skew_ms
        ):
            decision = StrategyDecision(False, f"feed skew gate ({skew_ms:.0f}ms)")
        row = self._opportunity_row(binding, state, decision, now_ns, skew_ms)
        self._last_rows[market_id] = row
        if not decision.accepted or decision.intent is None:
            return
        self._signals_seen += 1
        if self._runtime.paper_execution.portfolio.positions.get(market_id) is not None:
            self._signals_blocked_open_position += 1
            self._last_rows[market_id]["signal_state"] = "blocked: position open"
            return
        if self._paper_exec.has_pending_for_market(market_id):
            self._signals_blocked_open_position += 1
            self._last_rows[market_id]["signal_state"] = "blocked: order pending"
            return
        last_attempt_ns = self._last_attempt_ns.get(market_id, 0)
        cooldown_ns = self._runtime.config.paper_execution.signal_cooldown_ms * 1_000_000
        if now_ns - last_attempt_ns < cooldown_ns:
            self._signals_blocked_cooldown += 1
            self._last_rows[market_id]["signal_state"] = "blocked: cooldown"
            return
        pending = self._paper_exec.schedule(decision.intent)
        self._last_attempt_ns[market_id] = now_ns
        self._signals_attempted += 1
        self._recorder.emit(
            "paper_order_planned",
            {
                "ts_ns": decision.intent.decision_ts_ns,
                "market_id": market_id,
                "side": decision.intent.side.value,
                "signal_price": decision.intent.signal_price,
                "submit_ts_ns": pending.submit_ts_ns,
            },
        )

    def _opportunity_row(
        self,
        binding: LiveMarketBinding,
        state: StrategyMarketState,
        decision: StrategyDecision,
        now_ns: int,
        skew_ms: float | None,
    ) -> dict[str, Any]:
        divergence = None
        if state.coinbase_price and state.chainlink_price and state.chainlink_price.price > 0:
            divergence = (
                (state.coinbase_price.price - state.chainlink_price.price)
                / state.chainlink_price.price
            ) * 100.0
        selected_book = (
            state.yes_book if divergence is not None and divergence >= 0 else state.no_book
        )
        best_ask = selected_book.best_ask() if selected_book else None
        exec_price = None
        depth = None
        if decision.intent and selected_book:
            quote = self._runtime.execution_core.quote_buy(
                selected_book,
                signal_price=decision.intent.signal_price,
                target_shares=decision.intent.target_shares,
            )
            exec_price = quote.executable_price
            depth = quote.executable_shares
        freshness = []
        if state.coinbase_price:
            freshness.append(f"cb {state.coinbase_price.age_ms(now_ns):.0f}ms")
        if state.chainlink_price:
            freshness.append(f"cl {state.chainlink_price.age_ms(now_ns):.0f}ms")
        if selected_book:
            freshness.append(f"book {selected_book.age_ms(now_ns):.0f}ms")
        if skew_ms is not None:
            freshness.append(f"skew {skew_ms:.0f}ms")
        return {
            "coin": binding.coin,
            "interval": binding.interval.value,
            "market": binding.market_id,
            "side": decision.intent.side.value if decision.intent else "--",
            "minutes_to_close": round(binding.to_market_descriptor().minutes_to_close(now_ns), 4),
            "divergence_pct": None if divergence is None else round(divergence, 4),
            "best_ask": best_ask,
            "exec_price": exec_price,
            "depth_under_cap": depth,
            "freshness": " | ".join(freshness) if freshness else "--",
            "signal_state": "ready" if decision.accepted else decision.reason,
        }

    def _calibration(self) -> KellyCalibration:
        settled = self._settlements
        win_rate = self._wins / settled if settled > 0 else 0.0
        return KellyCalibration(trade_count=settled, win_rate=win_rate)

    def _feed_skew_ms(self, coin: str) -> float | None:
        coinbase = self._latest_coinbase.get(coin)
        chainlink = self._latest_chainlink.get(coin)
        if coinbase is None or chainlink is None:
            return None
        return abs(coinbase.source_event_ts_ns - chainlink.source_event_ts_ns) / 1_000_000.0

    def _all_token_ids(self) -> set[str]:
        token_ids: set[str] = set()
        for binding in self._markets.values():
            token_ids.add(binding.yes_token_id)
            token_ids.add(binding.no_token_id)
        return token_ids

    def _record_tick_latency(self, tick_ms: float) -> None:
        self._tick_count += 1
        self._last_tick_ms = tick_ms
        self._max_tick_ms = max(self._max_tick_ms, tick_ms)
        self._tick_avg_ms += (tick_ms - self._tick_avg_ms) / self._tick_count

    def _ensure_services(self) -> None:
        if self._ctx is not None and self._registry is not None and self._books is not None:
            return
        self._ctx = build_nautilus_support_context(loop=asyncio.get_running_loop())
        self._registry = MarketRegistry(
            http_client=self._ctx.http_client,
            config=self._runtime.config.market_registry,
            allowed_coins=self._runtime.config.markets,
        )
        self._books = PolymarketBookService(
            loop=self._ctx.loop,
            clock=self._ctx.clock,
            config=self._runtime.config.polymarket_market_ws,
            emit=self._emit,
            timeline=self._timeline,
        )

    def _write_state(self, *, status: str) -> None:
        now_ns = time.time_ns()
        if (
            status == "running"
            and (now_ns - self._last_flush_ns)
            < self._runtime.config.paper_execution.state_flush_interval_ms * 1_000_000
        ):
            return
        self._last_flush_ns = now_ns
        portfolio = self._runtime.paper_execution.portfolio
        tracker = self._tracker.snapshot()
        health = self._health.snapshot(
            now_ns=now_ns,
            feed_skew_ms_by_coin={
                coin: skew
                for coin in self._runtime.config.markets
                if (skew := self._feed_skew_ms(coin)) is not None
            },
        )
        rows = sorted(
            self._merged_opportunity_rows(),
            key=lambda row: (
                0 if row["signal_state"] == "ready" else 1,
                -(abs(_sortable_float(row["divergence_pct"], default=0.0))),
                _sortable_float(row["minutes_to_close"], default=999_999.0),
            ),
        )
        has_real_rows = any(
            row["signal_state"] != "awaiting live market" for row in rows
        )
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
        fills = len(tracker["fills"])
        rejects = len(tracker["rejects"])
        payload = {
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "status": status,
            "system": {
                "runtime_mode": "paper",
                "environment": self._runtime.config.environment,
                "markets": list(self._runtime.config.markets),
                "data_stack": [
                    "nautilus_polymarket_gamma_discovery",
                    "nautilus_polymarket_market_ws",
                    "coinbase_spot_ws",
                    "polymarket_rtds_ws",
                ],
                "execution_stack": ["realistic_polymarket_paper"],
                "feed_mode": "live",
                "state_path": self._runtime.config.runtime_state_path,
                "health": {
                    "feeds": {
                        name: {
                            "connected": status.connected,
                            "age_ms": status.age_ms(now_ns),
                            "reconnect_count": status.reconnect_count,
                            "detail": status.detail,
                        }
                        for name, status in health.feeds.items()
                    },
                    "feed_skew_ms_by_coin": health.feed_skew_ms_by_coin,
                    "stale_reasons": health.stale_reasons,
                },
            },
            "loop": {
                "avg_tick_ms": round(self._tick_avg_ms, 3),
                "max_tick_ms": round(self._max_tick_ms, 3),
                "last_tick_ms": round(self._last_tick_ms, 3),
                "uptime_s": round((now_ns - self._started_ns) / 1_000_000_000.0, 2),
            },
            "metrics": {
                "signals_seen": self._signals_seen,
                "signals_accepted": self._signals_attempted,
                "signals_blocked_open_position": self._signals_blocked_open_position,
                "signals_blocked_cooldown": self._signals_blocked_cooldown,
                "orders_submitted": len(tracker["orders"]),
                "fills": fills,
                "rejections": rejects,
                "settlements": self._settlements,
                "wins": self._wins,
                "losses": self._losses,
                "win_rate_pct": (
                    self._wins / self._settlements * 100.0 if self._settlements else 0.0
                ),
            },
            "portfolio": {
                "cash_balance_usd": round(portfolio.snapshot().cash_balance_usd, 4),
                "total_exposure_usd": round(portfolio.snapshot().total_exposure_usd, 4),
                "realized_pnl_usd": round(portfolio.realized_pnl_usd, 4),
                "open_positions": portfolio.snapshot().open_positions,
                "positions": positions,
                "mode_note": (
                    "Portfolio reflects real venue market data with delayed paper execution."
                ),
                "empty_state": "No positions are open yet.",
            },
            "execution": {
                **tracker,
                "notes": [
                    "Paper fills use the delayed submit-time real Polymarket book.",
                    "Settlement uses real market discovery and live resolution reconciliation.",
                ],
            },
            "opportunities": {
                "status": "live",
                "summary": (
                    "Live opportunities from real Polymarket/Coinbase/Chainlink feeds."
                    if has_real_rows
                    else "Tracking BTC/ETH 5m/15m slots; waiting for live market discovery."
                ),
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
                "rows": rows,
                "notes": [
                    (
                        "Rows are built from live Polymarket books, Coinbase spot, and RTDS "
                        "Chainlink feeds."
                    ),
                    (
                        "Execution still uses paper order lifecycle modeling; market state is "
                        "fully real."
                    ),
                ],
            },
        }
        self._store.write(payload)

    @staticmethod
    def _fmt_ts(ts_ns: int) -> str:
        return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=UTC).strftime("%H:%M:%S")

    def _merged_opportunity_rows(self) -> list[dict[str, Any]]:
        rows_by_slot: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._last_rows.values():
            rows_by_slot[(str(row["coin"]), self._interval_from_market_row(row))] = row
        for coin in self._runtime.config.markets:
            for interval in ("5m", "15m"):
                rows_by_slot.setdefault(
                    (coin, interval),
                    self._slot_placeholder_row(coin, interval),
                )
        return list(rows_by_slot.values())

    @staticmethod
    def _interval_from_market_row(row: dict[str, Any]) -> str:
        interval = str(row.get("interval") or "").lower()
        if interval in {"5m", "15m"}:
            return interval
        market_id = str(row.get("market") or "")
        return "15m" if "15m" in market_id.lower() else "5m"

    @staticmethod
    def _slot_placeholder_row(coin: str, interval: str) -> dict[str, Any]:
        return {
            "coin": coin,
            "interval": interval,
            "market": f"{coin} {interval}",
            "side": "awaiting bind",
            "minutes_to_close": "awaiting market",
            "divergence_pct": "awaiting feeds",
            "best_ask": "awaiting book",
            "exec_price": "awaiting book",
            "depth_under_cap": "awaiting book",
            "freshness": "awaiting market discovery",
            "signal_state": "awaiting live market",
        }


def _sortable_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
