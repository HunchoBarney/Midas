from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class RuntimeStateStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        with tmp_path.open("wb") as handle:
            handle.write(data)
        os.replace(tmp_path, self._path)

    def read(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict):
            return payload
        return None
