from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import msgspec
from nautilus_trader.adapters.polymarket.websocket.client import (
    PolymarketWebSocketChannel,
    PolymarketWebSocketClient,
)
from nautilus_trader.common.component import LiveClock

from cbcl_platform.config import PolymarketMarketWsConfig
from cbcl_platform.models import OrderBookLevel, OrderBookSnapshot
from cbcl_platform.paper import InMemoryBookTimeline


class PolymarketBookService:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        clock: LiveClock,
        config: PolymarketMarketWsConfig,
        emit: Callable[[dict[str, Any]], None],
        timeline: InMemoryBookTimeline,
    ) -> None:
        self._loop = loop
        self._config = config
        self._emit = emit
        self._timeline = timeline
        self._books: dict[str, dict[str, dict[float, float]]] = {}
        self._snapshots: dict[str, OrderBookSnapshot] = {}
        self._synced: set[str] = set()
        self._subscriptions: set[str] = set()
        self._resolution_hints: set[str] = set()
        self._client = PolymarketWebSocketClient(
            clock,
            base_url=config.base_url,
            channel=PolymarketWebSocketChannel.MARKET,
            handler=self._handle_raw_message,
            handler_reconnect=self._handle_reconnect,
            loop=loop,
            max_subscriptions_per_connection=config.ws_max_subscriptions_per_connection,
        )

    async def sync_tokens(self, token_ids: set[str]) -> None:
        add = token_ids - self._subscriptions
        remove = self._subscriptions - token_ids
        for token_id in remove:
            if self._client.is_connected():
                await self._client.unsubscribe(token_id)
            self._subscriptions.discard(token_id)
            self._synced.discard(token_id)
        for token_id in add:
            self._subscriptions.add(token_id)
            self._synced.discard(token_id)
            if self._client.is_connected():
                await self._client.subscribe(token_id)
            else:
                self._client.add_subscription(token_id)
        if self._subscriptions and self._client.is_disconnected():
            await asyncio.sleep(self._config.ws_connection_initial_delay_secs)
            await self._client.connect()

    async def stop(self) -> None:
        await self._client.disconnect()

    def snapshot(self, token_id: str) -> OrderBookSnapshot | None:
        return self._snapshots.get(token_id)

    def is_synced(self, token_id: str) -> bool:
        return token_id in self._synced

    def resolution_hints(self) -> set[str]:
        return set(self._resolution_hints)

    def clear_resolution_hint(self, market_id: str) -> None:
        self._resolution_hints.discard(market_id)

    async def _handle_reconnect(self) -> None:
        for token_id in list(self._subscriptions):
            self._synced.discard(token_id)
        self._emit(
            {
                "type": "feed_status",
                "feed": "polymarket_market",
                "connected": True,
                "detail": "reconnected",
            },
        )

    def _handle_raw_message(self, raw: bytes) -> None:
        payload = msgspec.json.decode(raw)
        if isinstance(payload, list):
            for item in payload:
                self._handle_message(item)
        elif isinstance(payload, dict):
            self._handle_message(payload)

    def _handle_message(self, message: dict[str, Any]) -> None:
        event_type = str(message.get("event_type") or "")
        if event_type == "book":
            self._handle_book(message)
            return
        if event_type == "price_change":
            self._handle_price_change(message)
            return
        if event_type == "market_resolved":
            market_id = str(message.get("market") or message.get("condition_id") or "")
            if market_id:
                self._resolution_hints.add(market_id)
                self._emit({"type": "resolution_hint", "market_id": market_id})
            return
        if event_type == "best_bid_ask":
            self._emit(
                {
                    "type": "feed_event",
                    "feed": "polymarket_market",
                    "ts_ns": time.time_ns(),
                },
            )

    def _handle_book(self, message: dict[str, Any]) -> None:
        token_id = str(message.get("asset_id") or "")
        if not token_id:
            return
        asks = {float(level["price"]): float(level["size"]) for level in message.get("asks", [])}
        bids = {float(level["price"]): float(level["size"]) for level in message.get("bids", [])}
        self._books[token_id] = {"asks": asks, "bids": bids}
        snapshot = self._build_snapshot(token_id, message.get("timestamp"))
        self._synced.add(token_id)
        self._emit({"type": "book_snapshot", "token_id": token_id, "snapshot": snapshot})

    def _handle_price_change(self, message: dict[str, Any]) -> None:
        changes = message.get("price_changes")
        if not isinstance(changes, list) or not changes:
            return
        token_id = str(changes[0].get("asset_id") or "")
        if not token_id or token_id not in self._books:
            return
        book = self._books[token_id]
        for change in changes:
            side = str(change.get("side") or "").upper()
            price = float(change.get("price") or 0.0)
            size = float(change.get("size") or 0.0)
            levels = book["bids"] if side == "BUY" else book["asks"]
            if size <= 0.0:
                levels.pop(price, None)
            else:
                levels[price] = size
        snapshot = self._build_snapshot(token_id, message.get("timestamp"))
        self._emit({"type": "book_snapshot", "token_id": token_id, "snapshot": snapshot})

    def _build_snapshot(self, token_id: str, timestamp: Any) -> OrderBookSnapshot:
        now_ns = time.time_ns()
        ts_event_ns = int(float(timestamp or 0)) * 1_000_000 if timestamp is not None else now_ns
        book = self._books[token_id]
        snapshot = OrderBookSnapshot(
            token_id=token_id,
            asks=tuple(
                OrderBookLevel(price=price, size=size)
                for price, size in sorted(book["asks"].items(), key=lambda item: item[0])
            ),
            bids=tuple(
                OrderBookLevel(price=price, size=size)
                for price, size in sorted(
                    book["bids"].items(),
                    key=lambda item: item[0],
                    reverse=True,
                )
            ),
            source_event_ts_ns=ts_event_ns,
            local_receive_ts_ns=now_ns,
        )
        self._snapshots[token_id] = snapshot
        self._timeline.add_snapshot(token_id, now_ns, snapshot)
        self._emit({"type": "feed_event", "feed": "polymarket_market", "ts_ns": now_ns})
        return snapshot
