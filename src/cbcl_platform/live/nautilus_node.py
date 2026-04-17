from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from nautilus_trader.common.component import LiveClock


@dataclass(frozen=True)
class NautilusSupportContext:
    loop: asyncio.AbstractEventLoop
    clock: LiveClock
    http_client: httpx.AsyncClient


def build_nautilus_support_context(
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    timeout_secs: float = 30.0,
) -> NautilusSupportContext:
    loop = loop or asyncio.get_event_loop()
    return NautilusSupportContext(
        loop=loop,
        clock=LiveClock(),
        http_client=httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_secs),
            follow_redirects=True,
        ),
    )
