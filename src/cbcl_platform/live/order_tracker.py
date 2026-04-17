from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

from cbcl_platform.models import ExecutionStatus, OrderLifecycle


def _fmt_ts(ts_ns: int | None) -> str:
    if ts_ns is None:
        return "--"
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=UTC).strftime("%H:%M:%S")


class OrderTracker:
    def __init__(self) -> None:
        self.orders: deque[dict[str, Any]] = deque(maxlen=128)
        self.fills: deque[dict[str, Any]] = deque(maxlen=128)
        self.rejects: deque[dict[str, Any]] = deque(maxlen=128)
        self.settlements: deque[dict[str, Any]] = deque(maxlen=128)

    def record_lifecycle(self, lifecycle: OrderLifecycle) -> None:
        submit_ms = max(
            0,
            int((lifecycle.submit_ts_ns - lifecycle.decision_ts_ns) / 1_000_000),
        )
        ack_ms = (
            None
            if lifecycle.ack_ts_ns is None
            else max(0, int((lifecycle.ack_ts_ns - lifecycle.submit_ts_ns) / 1_000_000))
        )
        confirm_ms = (
            None
            if lifecycle.confirmed_ts_ns is None or lifecycle.fill_ts_ns is None
            else max(0, int((lifecycle.confirmed_ts_ns - lifecycle.fill_ts_ns) / 1_000_000))
        )
        row = {
            "time": _fmt_ts(lifecycle.decision_ts_ns),
            "market": lifecycle.market_id,
            "market_id": lifecycle.market_id,
            "side": lifecycle.side.value,
            "status": lifecycle.status.value,
            "limit_price": lifecycle.limit_price,
            "requested_shares": lifecycle.requested_shares,
            "filled_shares": lifecycle.fill.filled_shares,
            "average_price": lifecycle.fill.average_price,
            "total_cost": lifecycle.fill.total_cost,
            "trade_fee_usd": lifecycle.fill.trade_fee_usd,
            "submit_ms": submit_ms,
            "ack_ms": ack_ms,
            "confirm_ms": confirm_ms,
            "reason": lifecycle.reason,
        }
        self.orders.appendleft(row)
        if lifecycle.status in {ExecutionStatus.FILLED, ExecutionStatus.PARTIAL}:
            self.fills.appendleft(row)
        if lifecycle.status == ExecutionStatus.REJECTED:
            self.rejects.appendleft(row)

    def record_settlement(self, row: dict[str, Any]) -> None:
        self.settlements.appendleft(row)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "orders": list(self.orders),
            "fills": list(self.fills),
            "rejects": list(self.rejects),
            "settlements": list(self.settlements),
        }
