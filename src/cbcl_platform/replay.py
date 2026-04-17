from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from cbcl_platform.runtime import TradingRuntime


def replay_summary(runtime: TradingRuntime) -> dict[str, Any]:
    path = Path(runtime.config.recorder.path)
    if not path.exists():
        return {
            "status": "empty",
            "path": str(path),
            "summary": "No replay data recorded yet.",
            "event_count": 0,
            "event_types": {},
            "latest_events": [],
        }

    event_types: Counter[str] = Counter()
    latest_events: list[dict[str, Any]] = []
    first_recorded_at: str | None = None
    last_recorded_at: str | None = None
    event_count = 0

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            event_count += 1
            event_type = str(payload.get("type") or "unknown")
            event_types[event_type] += 1
            recorded_at = payload.get("recorded_at")
            if first_recorded_at is None:
                first_recorded_at = recorded_at
            last_recorded_at = recorded_at
            latest_events.append(payload)
            if len(latest_events) > 10:
                latest_events = latest_events[-10:]

    return {
        "status": "ready",
        "path": str(path),
        "summary": "Recorded real-feed runtime events for replay and audit.",
        "event_count": event_count,
        "event_types": dict(sorted(event_types.items())),
        "first_recorded_at": first_recorded_at,
        "last_recorded_at": last_recorded_at,
        "latest_events": latest_events,
    }
