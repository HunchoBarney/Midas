from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import msgspec
import websockets

from cbcl_platform.config import RtdsWsConfig
from cbcl_platform.models import PriceUpdate


class RtdsDataClient:
    def __init__(
        self,
        *,
        config: RtdsWsConfig,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self._config = config
        self._emit = emit
        self._running = True
        self._connected_symbols: set[str] = set()

    async def run(self) -> None:
        self._emit({"type": "feed_status", "feed": "chainlink", "connected": False})
        tasks = [
            asyncio.create_task(self._run_symbol(symbol), name=f"rtds-{symbol}")
            for symbol in ("btc/usd", "eth/usd")
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _ping_loop(self, ws) -> None:  # type: ignore[no-untyped-def]
        while True:
            await asyncio.sleep(self._config.ping_interval_secs)
            await ws.send("PING")

    def stop(self) -> None:
        self._running = False

    @staticmethod
    def _subscription_payloads() -> tuple[str, ...]:
        return tuple(
            RtdsDataClient._subscription_payload(symbol) for symbol in ("btc/usd", "eth/usd")
        )

    async def _run_symbol(self, symbol: str) -> None:
        payload = self._subscription_payload(symbol)
        while self._running:
            try:
                async with websockets.connect(self._config.base_url, ping_interval=None) as ws:
                    self._mark_symbol_connected(symbol)
                    await ws.send(payload)
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            if raw == "PONG" or raw == "":
                                continue
                            if isinstance(raw, str):
                                raw = raw.encode()
                            message = msgspec.json.decode(raw)
                            event = self._parse_message(message)
                            if event:
                                self._emit(event)
                    finally:
                        ping_task.cancel()
            except Exception as exc:  # noqa: BLE001
                self._mark_symbol_disconnected(symbol, detail=str(exc))
                if self._running:
                    await asyncio.sleep(self._config.reconnect_delay_secs)
            else:
                self._mark_symbol_disconnected(symbol, detail="")

    def _mark_symbol_connected(self, symbol: str) -> None:
        already_connected = bool(self._connected_symbols)
        self._connected_symbols.add(symbol)
        if not already_connected:
            self._emit({"type": "feed_status", "feed": "chainlink", "connected": True})

    def _mark_symbol_disconnected(self, symbol: str, *, detail: str) -> None:
        was_connected = bool(self._connected_symbols)
        self._connected_symbols.discard(symbol)
        if was_connected and not self._connected_symbols:
            self._emit(
                {
                    "type": "feed_status",
                    "feed": "chainlink",
                    "connected": False,
                    "detail": detail,
                },
            )

    @staticmethod
    def _subscription_payload(symbol: str) -> str:
        return msgspec.json.encode(
            {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": f'{{"symbol":"{symbol}"}}',
                    },
                ],
            },
        ).decode()

    @staticmethod
    def _parse_message(message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return None
        topic = str(message.get("topic") or "")
        msg_type = str(message.get("type") or "")
        payload = message.get("payload")
        if topic == "crypto_prices" and msg_type == "subscribe":
            return RtdsDataClient._parse_subscribe_snapshot(payload)
        if topic != "crypto_prices_chainlink":
            return None
        if msg_type != "update":
            return {
                "type": "feed_event",
                "feed": "chainlink",
                "ts_ns": time.time_ns(),
            }
        return RtdsDataClient._parse_price_payload(payload)

    @staticmethod
    def _parse_subscribe_snapshot(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        symbol = str(payload.get("symbol") or "").lower()
        data = payload.get("data")
        if symbol not in {"btc/usd", "eth/usd"} or not isinstance(data, list) or not data:
            return None
        latest = max(
            (item for item in data if isinstance(item, dict)),
            key=lambda item: int(item.get("timestamp", 0)),
            default=None,
        )
        if latest is None:
            return None
        return RtdsDataClient._parse_price_payload(
            {
                "symbol": symbol,
                "timestamp": latest.get("timestamp"),
                "value": latest.get("value"),
            },
        )

    @staticmethod
    def _parse_price_payload(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        symbol = str(payload.get("symbol") or "").lower()
        if symbol not in {"btc/usd", "eth/usd"}:
            return None
        event_ns = int(payload.get("timestamp", 0)) * 1_000_000
        now_ns = time.time_ns()
        update = PriceUpdate(
            source="chainlink",
            symbol=symbol.split("/", 1)[0].upper(),
            price=float(payload["value"]),
            source_event_ts_ns=event_ns,
            local_receive_ts_ns=now_ns,
        )
        return {
            "type": "chainlink_price",
            "coin": update.symbol,
            "update": update,
        }
