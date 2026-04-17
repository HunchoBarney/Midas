from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import msgspec
import websockets

from cbcl_platform.config import CoinbaseWsConfig
from cbcl_platform.models import PriceUpdate


class CoinbaseDataClient:
    def __init__(
        self,
        *,
        config: CoinbaseWsConfig,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self._config = config
        self._emit = emit
        self._running = True

    async def run(self) -> None:
        products = ["BTC-USD", "ETH-USD"]
        while self._running:
            self._emit({"type": "feed_status", "feed": "coinbase", "connected": False})
            try:
                async with websockets.connect(self._config.base_url, ping_interval=None) as ws:
                    self._emit({"type": "feed_status", "feed": "coinbase", "connected": True})
                    await ws.send(
                        msgspec.json.encode(
                            {
                                "type": "subscribe",
                                "product_ids": products,
                                "channels": ["ticker", "heartbeat"],
                            },
                        ).decode(),
                    )
                    async for raw in ws:
                        if isinstance(raw, str):
                            raw = raw.encode()
                        message = msgspec.json.decode(raw)
                        event = self._parse_message(message)
                        if event:
                            self._emit(event)
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    {
                        "type": "feed_status",
                        "feed": "coinbase",
                        "connected": False,
                        "detail": str(exc),
                    },
                )
                await asyncio.sleep(self._config.reconnect_delay_secs)

    def stop(self) -> None:
        self._running = False

    @staticmethod
    def _parse_message(message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return None
        msg_type = str(message.get("type") or "")
        now_ns = time_ns()
        if msg_type == "heartbeat":
            return {"type": "feed_event", "feed": "coinbase", "ts_ns": now_ns}
        if msg_type != "ticker":
            return None
        product_id = str(message.get("product_id") or "")
        coin = product_id.split("-", 1)[0]
        price = message.get("price")
        ts_text = message.get("time")
        if not coin or price is None or ts_text is None:
            return None
        event_ns = _parse_iso_to_ns(str(ts_text))
        update = PriceUpdate(
            source="coinbase",
            symbol=coin,
            price=float(price),
            source_event_ts_ns=event_ns,
            local_receive_ts_ns=now_ns,
            volume_24h=(
                float(message["volume_24h"])
                if message.get("volume_24h") is not None
                else None
            ),
        )
        return {
            "type": "coinbase_price",
            "coin": coin,
            "update": update,
        }


def _parse_iso_to_ns(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9)


def time_ns() -> int:
    return int(datetime.now(UTC).timestamp() * 1e9)
