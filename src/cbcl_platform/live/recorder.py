from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from cbcl_platform.config import RecorderConfig


class Recorder:
    def __init__(self, config: RecorderConfig) -> None:
        self._config = config
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=10_000)
        self._task: asyncio.Task[None] | None = None
        self._dropped = 0

    async def start(self) -> None:
        if not self._config.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="cbcl-recorder")

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._config.enabled:
            return
        event = {"type": event_type, **payload}
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1

    @property
    def dropped_count(self) -> int:
        return self._dropped

    async def _run(self) -> None:
        path = Path(self._config.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            item = await self._queue.get()
            if item is None:
                break
            line = json.dumps(item, sort_keys=True)
            await asyncio.to_thread(self._append_line, path, line)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
