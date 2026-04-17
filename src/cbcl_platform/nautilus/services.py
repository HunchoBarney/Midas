from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cbcl_platform.live.market_registry import MarketRegistry
from cbcl_platform.models import (
    FeedStatus,
    LiveMarketBinding,
    OrderLifecycle,
    PriceUpdate,
    RuntimeMode,
)
from cbcl_platform.nautilus.recorder import RuntimeRecorder
from cbcl_platform.paper import InMemoryBookTimeline, RealisticPaperExecutionAdapter
from cbcl_platform.runtime import TradingRuntime
from cbcl_platform.state_store import RuntimeStateStore

_PRICE_HISTORY_RETENTION_NS = 10 * 60 * 1_000_000_000
_SHORT_MOVE_WINDOW_NS = 60 * 1_000_000_000
_RECORDED_FEED_EVENT_MIN_INTERVAL_NS = 250 * 1_000_000


@dataclass
class RuntimeServices:
    runtime_id: str
    runtime: TradingRuntime
    state_store: RuntimeStateStore
    registry: MarketRegistry
    timeline: InMemoryBookTimeline = field(default_factory=InMemoryBookTimeline)
    paper_execution: RealisticPaperExecutionAdapter | None = None
    recorder: RuntimeRecorder | None = None
    bindings: dict[str, LiveMarketBinding] = field(default_factory=dict)
    latest_coinbase: dict[str, PriceUpdate] = field(default_factory=dict)
    latest_chainlink: dict[str, PriceUpdate] = field(default_factory=dict)
    latest_coinbase_volume_24h: dict[str, float] = field(default_factory=dict)
    coinbase_history: dict[str, deque[PriceUpdate]] = field(default_factory=dict)
    chainlink_history: dict[str, deque[PriceUpdate]] = field(default_factory=dict)
    feed_status: dict[str, FeedStatus] = field(default_factory=dict)
    feed_skew_ms_by_coin: dict[str, float] = field(default_factory=dict)
    stale_reasons: dict[str, str] = field(default_factory=dict)
    opportunity_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    order_lifecycles: deque[OrderLifecycle] = field(default_factory=lambda: deque(maxlen=100))
    settlements: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=100))
    status: str = "starting"
    signals_seen: int = 0
    signals_attempted: int = 0
    signals_blocked_open_position: int = 0
    signals_blocked_cooldown: int = 0
    wins: int = 0
    losses: int = 0
    last_flush_ns: int = 0
    persist_state: bool = True
    last_recorded_feed_event_ns: dict[str, int] = field(default_factory=dict)
    startup_metrics: dict[str, float | int | bool | str] = field(default_factory=dict)
    first_data_ts_ns: dict[str, int] = field(default_factory=dict)

    @property
    def mode(self) -> RuntimeMode:
        return self.runtime.mode

    def set_bindings(self, bindings: dict[str, LiveMarketBinding]) -> None:
        self.bindings = dict(bindings)
        active_market_ids = set(self.bindings)
        self.opportunity_rows = {
            market_id: row
            for market_id, row in self.opportunity_rows.items()
            if market_id in active_market_ids
        }

    def set_status(self, status: str) -> None:
        self.status = status

    def record_startup_metric(self, name: str, value: float | int | bool | str) -> None:
        self.startup_metrics[name] = value
        self._record("startup_metric", {"name": name, "value": value})

    def mark_feed_connected(self, name: str, *, detail: str = "") -> None:
        existing = self.feed_status.get(name)
        reconnects = 0 if existing is None else existing.reconnect_count
        if existing is not None and not existing.connected:
            reconnects += 1
        self.feed_status[name] = FeedStatus(
            name=name,
            connected=True,
            last_event_ts_ns=existing.last_event_ts_ns if existing else None,
            reconnect_count=reconnects,
            detail=detail,
        )
        self._record("feed_status", {"feed": name, "connected": True, "detail": detail})

    def mark_feed_disconnected(self, name: str, *, detail: str = "") -> None:
        existing = self.feed_status.get(name)
        reconnects = 0 if existing is None else existing.reconnect_count
        self.feed_status[name] = FeedStatus(
            name=name,
            connected=False,
            last_event_ts_ns=existing.last_event_ts_ns if existing else None,
            reconnect_count=reconnects,
            detail=detail,
        )
        self._record("feed_status", {"feed": name, "connected": False, "detail": detail})

    def mark_feed_event(self, name: str, ts_ns: int) -> None:
        existing = self.feed_status.get(name)
        self.feed_status[name] = FeedStatus(
            name=name,
            connected=True if existing is None else existing.connected,
            last_event_ts_ns=ts_ns,
            reconnect_count=0 if existing is None else existing.reconnect_count,
            detail="" if existing is None else existing.detail,
        )
        last_recorded = self.last_recorded_feed_event_ns.get(name)
        if (
            last_recorded is not None
            and ts_ns - last_recorded < _RECORDED_FEED_EVENT_MIN_INTERVAL_NS
        ):
            return
        self.last_recorded_feed_event_ns[name] = ts_ns
        self._record("feed_event", {"feed": name, "ts_ns": ts_ns})

    def record_polymarket_book_event(self, ts_ns: int) -> None:
        self.mark_feed_event("polymarket_market", ts_ns)
        self.first_data_ts_ns.setdefault("polymarket_book", ts_ns)

    def record_coinbase(self, update: PriceUpdate) -> None:
        coin = update.symbol.upper()
        self.latest_coinbase[coin] = update
        self.first_data_ts_ns.setdefault("coinbase_price", update.local_receive_ts_ns)
        if update.volume_24h is not None:
            self.latest_coinbase_volume_24h[coin] = float(update.volume_24h)
        self._append_price_history(self.coinbase_history, coin, update)
        self.mark_feed_event("coinbase", update.local_receive_ts_ns)
        self._record(
            "coinbase_price",
            {
                "symbol": update.symbol,
                "price": update.price,
                "volume_24h": update.volume_24h,
                "source_event_ts_ns": update.source_event_ts_ns,
                "local_receive_ts_ns": update.local_receive_ts_ns,
            },
        )

    def record_chainlink(self, update: PriceUpdate) -> None:
        coin = update.symbol.upper()
        self.latest_chainlink[coin] = update
        self.first_data_ts_ns.setdefault("chainlink_price", update.local_receive_ts_ns)
        self._append_price_history(self.chainlink_history, coin, update)
        self.mark_feed_event("chainlink", update.local_receive_ts_ns)
        self._record(
            "chainlink_price",
            {
                "symbol": update.symbol,
                "price": update.price,
                "source_event_ts_ns": update.source_event_ts_ns,
                "local_receive_ts_ns": update.local_receive_ts_ns,
            },
        )

    def record_opportunity_row(self, market_id: str, row: dict[str, Any]) -> None:
        self.opportunity_rows[market_id] = dict(row)
        self._record("opportunity_row", {"market_id": market_id, "row": dict(row)})

    def record_order_lifecycle(self, lifecycle: OrderLifecycle) -> None:
        self.order_lifecycles.appendleft(lifecycle)
        self._record("order_lifecycle", self._lifecycle_payload(lifecycle))

    def record_settlement(self, payload: dict[str, Any]) -> None:
        self.settlements.appendleft(dict(payload))
        pnl = float(payload.get("pnl_usd", 0.0))
        if pnl >= 0.0:
            self.wins += 1
        else:
            self.losses += 1
        self._record("settlement", dict(payload))

    def current_portfolio(self) -> dict[str, Any]:
        if self.paper_execution is None:
            return {
                "cash_balance_usd": 0.0,
                "total_exposure_usd": 0.0,
                "realized_pnl_usd": 0.0,
                "open_positions": 0,
                "positions": [],
                "mode_note": "Live portfolio reporting is not fully wired yet.",
            }

        portfolio = self.paper_execution.portfolio
        snapshot = portfolio.snapshot()
        positions: list[dict[str, Any]] = []
        for market_id, position in sorted(portfolio.positions.items()):
            positions.append(
                {
                    "market_id": market_id,
                    "yes_shares": round(position.yes_shares, 8),
                    "no_shares": round(position.no_shares, 8),
                    "cost_basis_usd": round(position.cost_basis_usd, 8),
                    "fees_paid_usd": round(position.fees_paid_usd, 8),
                }
            )
        return {
            "cash_balance_usd": round(snapshot.cash_balance_usd, 8),
            "total_exposure_usd": round(snapshot.total_exposure_usd, 8),
            "realized_pnl_usd": round(portfolio.realized_pnl_usd, 8),
            "open_positions": snapshot.open_positions,
            "positions": positions,
            "mode_note": "Portfolio reflects the Nautilus paper execution ledger.",
        }

    def snapshot_payload(self) -> dict[str, Any]:
        now_ns = time_ns()
        portfolio = self.current_portfolio()
        fills = [
            self._lifecycle_payload(item)
            for item in self.order_lifecycles
            if item.status.value in {"filled", "partial"}
        ]
        rejects = [
            self._lifecycle_payload(item)
            for item in self.order_lifecycles
            if item.status.value == "rejected"
        ]
        orders = [self._lifecycle_payload(item) for item in self.order_lifecycles]
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "runtime_id": self.runtime_id,
            "status": self.status,
            "system": {
                "runtime_mode": self.mode.value,
                "feed_mode": "live",
                "latency_mode": "nautilus_node",
                "runtime_id": self.runtime_id,
                "state_path": str(self.state_store.path),
                "data_stack": [
                    "nautilus_polymarket_public_data",
                    "nautilus_coinbase_spot",
                    "nautilus_rtds_chainlink",
                ],
                "execution_stack": [
                    "nautilus_polymarket_paper"
                    if self.mode == RuntimeMode.PAPER
                    else "nautilus_polymarket_live"
                ],
                "bindings": [self._binding_payload(binding) for binding in self.bindings.values()],
                "startup": {
                    "metrics": dict(sorted(self.startup_metrics.items())),
                    "first_data_ts_ns": dict(sorted(self.first_data_ts_ns.items())),
                },
                "feeds": {
                    name: {
                        "connected": status.connected,
                        "last_event_ts_ns": status.last_event_ts_ns,
                        "age_ms": status.age_ms(now_ns),
                        "state": self._feed_state(name, status, now_ns),
                        "reconnect_count": status.reconnect_count,
                        "detail": status.detail,
                    }
                    for name, status in sorted(self.feed_status.items())
                },
                "feed_skew_ms_by_coin": {
                    coin: round(value, 3)
                    for coin, value in sorted(self.feed_skew_ms_by_coin.items())
                },
                "stale_reasons": dict(sorted(self.stale_reasons.items())),
                "summary": (
                    "Nautilus trading node is the runtime kernel for both paper and live modes."
                ),
            },
            "portfolio": portfolio,
            "execution": {
                "orders_submitted": len(orders),
                "fills": fills,
                "rejects": rejects,
                "orders": orders,
                "settlements": list(self.settlements),
            },
            "opportunities": {
                "status": "live",
                "summary": "Current market opportunities ranked by readiness and divergence.",
                "monitor_summary": (
                    "BTC/ETH live feed monitor stays populated even when no tradable "
                    "UpDown contract is currently bound."
                ),
                "notes": (
                    "Rows are produced by the Nautilus strategy using real feeds and "
                    "real Polymarket books."
                ),
                "monitor_columns": [
                    "coin",
                    "active_market",
                    "spot_price",
                    "oracle_price",
                    "divergence_pct",
                    "spot_move_1m_pct",
                    "oracle_move_1m_pct",
                    "volume_24h",
                    "feed_skew_ms",
                    "freshness",
                    "market_state",
                ],
                "monitor_rows": self._market_monitor_rows(now_ns),
                "columns": [
                    "coin",
                    "market",
                    "side",
                    "minutes_to_close",
                    "divergence_pct",
                    "best_ask",
                    "exec_price",
                    "depth_under_cap",
                    "signal_state",
                    "reason",
                ],
                "rows": sorted(
                    self.opportunity_rows.values(),
                    key=lambda row: (str(row.get("coin")), str(row.get("market"))),
                ),
            },
            "runtime": {
                "signals_seen": self.signals_seen,
                "signals_attempted": self.signals_attempted,
                "signals_blocked_open_position": self.signals_blocked_open_position,
                "signals_blocked_cooldown": self.signals_blocked_cooldown,
                "wins": self.wins,
                "losses": self.losses,
                "recorder_dropped": 0 if self.recorder is None else self.recorder.dropped_count,
                "last_flush_ns": now_ns,
            },
        }
        return payload

    def write_state(self, *, force: bool = False) -> None:
        now_ns = time_ns()
        flush_interval_ns = self.runtime.config.paper_execution.state_flush_interval_ms * 1_000_000
        if not force and now_ns - self.last_flush_ns < flush_interval_ns:
            return
        if not self.persist_state and not force:
            self.last_flush_ns = now_ns
            return

        payload = self.snapshot_payload()
        self.state_store.write(payload)
        self._record(
            "state_snapshot",
            {
                "status": payload["status"],
                "generated_at": payload["generated_at"],
                "runtime_mode": payload["system"]["runtime_mode"],
            },
        )
        self.last_flush_ns = now_ns

    def close(self) -> None:
        if self.recorder is not None:
            self.recorder.close()

    @staticmethod
    def _binding_payload(binding: LiveMarketBinding) -> dict[str, Any]:
        return {
            "market_id": binding.market_id,
            "condition_id": binding.condition_id,
            "event_slug": binding.event_slug,
            "coin": binding.coin,
            "interval": binding.interval.value,
            "expires_at_ns": binding.expires_at_ns,
            "yes_token_id": binding.yes_token_id,
            "no_token_id": binding.no_token_id,
        }

    @staticmethod
    def _lifecycle_payload(item: OrderLifecycle) -> dict[str, Any]:
        return {
            "order_id": item.order_id,
            "market_id": item.market_id,
            "token_id": item.token_id,
            "side": item.side.value,
            "status": item.status.value,
            "reason": item.reason,
            "decision_ts_ns": item.decision_ts_ns,
            "submit_ts_ns": item.submit_ts_ns,
            "ack_ts_ns": item.ack_ts_ns,
            "fill_ts_ns": item.fill_ts_ns,
            "confirmed_ts_ns": item.confirmed_ts_ns,
            "limit_price": item.limit_price,
            "requested_shares": item.requested_shares,
            "filled_shares": item.fill.filled_shares,
            "average_price": item.fill.average_price,
            "total_cost": item.fill.total_cost,
            "trade_fee_usd": item.fill.trade_fee_usd,
            "gas_fee_usd": item.fill.gas_fee_usd,
            "metadata": item.metadata,
        }

    def _record(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.recorder is None:
            return
        self.recorder.emit(
            event_type,
            {
                "runtime_id": self.runtime_id,
                "mode": self.mode.value,
                **payload,
            },
        )

    @staticmethod
    def _append_price_history(
        bucket: dict[str, deque[PriceUpdate]],
        coin: str,
        update: PriceUpdate,
    ) -> None:
        history = bucket.setdefault(coin, deque(maxlen=2048))
        history.append(update)
        cutoff_ns = update.local_receive_ts_ns - _PRICE_HISTORY_RETENTION_NS
        while history and history[0].local_receive_ts_ns < cutoff_ns:
            history.popleft()

    @staticmethod
    def _move_pct(
        bucket: dict[str, deque[PriceUpdate]],
        coin: str,
        now_ns: int,
    ) -> float | None:
        history = bucket.get(coin)
        if not history or len(history) < 2:
            return None
        latest = history[-1]
        cutoff_ns = now_ns - _SHORT_MOVE_WINDOW_NS
        baseline = None
        for point in history:
            if point.local_receive_ts_ns >= cutoff_ns:
                baseline = point
                break
        if baseline is None:
            baseline = history[0]
        if baseline.price <= 0.0:
            return None
        return ((latest.price - baseline.price) / baseline.price) * 100.0

    def _nearest_binding_for_coin(self, coin: str, now_ns: int) -> LiveMarketBinding | None:
        candidates = [
            binding
            for binding in self.bindings.values()
            if binding.coin.upper() == coin.upper() and not binding.resolved
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda binding: (
                binding.expires_at_ns < now_ns,
                abs(binding.expires_at_ns - now_ns),
            ),
        )

    def _market_monitor_rows(self, now_ns: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for coin in self.runtime.config.markets:
            upper_coin = coin.upper()
            coinbase = self.latest_coinbase.get(upper_coin)
            chainlink = self.latest_chainlink.get(upper_coin)
            binding = self._nearest_binding_for_coin(upper_coin, now_ns)
            divergence_pct = None
            if coinbase is not None and chainlink is not None and chainlink.price > 0.0:
                divergence_pct = ((coinbase.price - chainlink.price) / chainlink.price) * 100.0
            freshness_parts: list[str] = []
            if coinbase is not None:
                freshness_parts.append(f"cb {coinbase.age_ms(now_ns):.0f}ms")
            else:
                freshness_parts.append("cb missing")
            if chainlink is not None:
                freshness_parts.append(f"cl {chainlink.age_ms(now_ns):.0f}ms")
            else:
                freshness_parts.append("cl missing")
            stale_reason = self.stale_reasons.get(upper_coin)
            active_row = (
                self.opportunity_rows.get(binding.market_id)
                if binding is not None
                else None
            )
            market_state = stale_reason
            if market_state is None and active_row and active_row.get("reason"):
                market_state = str(active_row.get("reason"))
            if market_state is None:
                market_state = "watching live feeds" if binding is None else "market bound"
            spot_move = self._move_pct(self.coinbase_history, upper_coin, now_ns)
            oracle_move = self._move_pct(self.chainlink_history, upper_coin, now_ns)
            rows.append(
                {
                    "coin": upper_coin,
                    "active_market": (
                        f"{binding.coin} {binding.interval.value}"
                        if binding is not None
                        else "no active updown"
                    ),
                    "minutes_to_close": (
                        round(binding.to_market_descriptor().minutes_to_close(now_ns), 3)
                        if binding is not None
                        else None
                    ),
                    "spot_price": None if coinbase is None else round(coinbase.price, 4),
                    "oracle_price": None if chainlink is None else round(chainlink.price, 4),
                    "divergence_pct": (
                        None if divergence_pct is None else round(divergence_pct, 4)
                    ),
                    "spot_move_1m_pct": None if spot_move is None else round(spot_move, 4),
                    "oracle_move_1m_pct": (
                        None
                        if oracle_move is None
                        else round(oracle_move, 4)
                    ),
                    "volume_24h": self.latest_coinbase_volume_24h.get(upper_coin),
                    "feed_skew_ms": (
                        None
                        if upper_coin not in self.feed_skew_ms_by_coin
                        else round(self.feed_skew_ms_by_coin[upper_coin], 1)
                    ),
                    "freshness": " | ".join(freshness_parts),
                    "market_state": market_state,
                }
            )
        return rows

    def _feed_state(self, name: str, status: FeedStatus, now_ns: int) -> str:
        if not status.connected:
            return "reconnecting"
        age_ms = status.age_ms(now_ns)
        max_age = self._max_age_for(name)
        if age_ms is None:
            return "warmup"
        if max_age is not None and age_ms > max_age:
            return "stale"
        return "healthy"

    def _max_age_for(self, name: str) -> int | None:
        freshness = self.runtime.config.freshness
        if name == "polymarket_market":
            return freshness.polymarket_book_age_ms
        if name == "coinbase":
            return freshness.coinbase_age_ms
        if name == "chainlink":
            return freshness.chainlink_age_ms
        return None


_SERVICES: dict[str, RuntimeServices] = {}


def register_runtime_services(services: RuntimeServices) -> None:
    _SERVICES[services.runtime_id] = services


def get_runtime_services(runtime_id: str) -> RuntimeServices:
    return _SERVICES[runtime_id]


def lookup_runtime_services(runtime_id: str | None) -> RuntimeServices | None:
    if not runtime_id:
        return None
    return _SERVICES.get(runtime_id)


def unregister_runtime_services(runtime_id: str) -> None:
    _SERVICES.pop(runtime_id, None)


def time_ns() -> int:
    return int(datetime.now(UTC).timestamp() * 1e9)
