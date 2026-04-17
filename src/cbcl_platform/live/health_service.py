from __future__ import annotations

import time
from dataclasses import replace

from cbcl_platform.config import FreshnessConfig
from cbcl_platform.models import FeedStatus, RuntimeHealthSnapshot


class HealthService:
    def __init__(self, *, freshness: FreshnessConfig) -> None:
        self._freshness = freshness
        self._feeds: dict[str, FeedStatus] = {}

    def mark_connected(self, name: str, *, detail: str = "") -> None:
        current = self._feeds.get(name)
        reconnects = current.reconnect_count if current else 0
        if current and not current.connected:
            reconnects += 1
        self._feeds[name] = FeedStatus(
            name=name,
            connected=True,
            last_event_ts_ns=current.last_event_ts_ns if current else None,
            reconnect_count=reconnects,
            detail=detail,
        )

    def mark_disconnected(self, name: str, *, detail: str = "") -> None:
        current = self._feeds.get(name)
        self._feeds[name] = FeedStatus(
            name=name,
            connected=False,
            last_event_ts_ns=current.last_event_ts_ns if current else None,
            reconnect_count=current.reconnect_count if current else 0,
            detail=detail,
        )

    def mark_event(self, name: str, *, ts_ns: int | None = None, detail: str = "") -> None:
        current = self._feeds.get(name)
        connected = current.connected if current else True
        reconnect_count = current.reconnect_count if current else 0
        self._feeds[name] = FeedStatus(
            name=name,
            connected=connected,
            last_event_ts_ns=ts_ns or time.time_ns(),
            reconnect_count=reconnect_count,
            detail=detail or (current.detail if current else ""),
        )

    def snapshot(
        self,
        *,
        now_ns: int,
        feed_skew_ms_by_coin: dict[str, float] | None = None,
    ) -> RuntimeHealthSnapshot:
        stale_reasons: dict[str, str] = {}
        for name, status in self._feeds.items():
            age_ms = status.age_ms(now_ns)
            max_age = self._max_age_for(name)
            if not status.connected:
                stale_reasons[name] = "disconnected"
            elif age_ms is None:
                stale_reasons[name] = "no data yet"
            elif max_age is not None and age_ms > max_age:
                stale_reasons[name] = f"stale ({age_ms:.0f}ms > {max_age}ms)"
        return RuntimeHealthSnapshot(
            feeds={name: replace(status) for name, status in self._feeds.items()},
            feed_skew_ms_by_coin=dict(feed_skew_ms_by_coin or {}),
            stale_reasons=stale_reasons,
        )

    def _max_age_for(self, name: str) -> int | None:
        if name == "polymarket_market":
            return self._freshness.polymarket_book_age_ms
        if name == "coinbase":
            return self._freshness.coinbase_age_ms
        if name == "chainlink":
            return self._freshness.chainlink_age_ms
        return None
