from __future__ import annotations

import json
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cbcl_platform.config import RecorderConfig


class RuntimeRecorder:
    def __init__(self, config: RecorderConfig) -> None:
        self._config = config
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=10_000)
        self._thread: threading.Thread | None = None
        self._dropped = 0
        self._closed = False
        if self._config.enabled:
            self._start()

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._config.enabled or self._closed:
            return
        event = {
            "type": event_type,
            "recorded_at": datetime.now(UTC).isoformat(),
            **payload,
        }
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1

    def close(self) -> None:
        if not self._config.enabled or self._closed:
            return
        self._closed = True
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="cbcl-nautilus-recorder",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        path = Path(self._config.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                batch: list[dict[str, Any]] = [item]
                close_after_batch = False
                while True:
                    try:
                        queued = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if queued is None:
                        close_after_batch = True
                        break
                    batch.append(queued)

                for event in batch:
                    handle.write(json.dumps(event, sort_keys=True))
                    handle.write("\n")
                handle.flush()

                if close_after_batch:
                    break
