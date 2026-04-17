from __future__ import annotations

import sys
import types
from typing import Any


class _ShimDataFrame:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._payload: Any = None

    def to_json(self) -> str:
        return "[]"


class _ShimTimestamp:
    def __init__(self, value: Any = None, *_args: Any, **_kwargs: Any) -> None:
        self._value = value

    def isoformat(self) -> str:
        return "" if self._value is None else str(self._value)

    def __str__(self) -> str:
        return self.isoformat()


class _ShimTimedelta:
    def __init__(self, value: Any = None, *_args: Any, **_kwargs: Any) -> None:
        self._value = value

    def isoformat(self) -> str:
        return "" if self._value is None else str(self._value)

    def __str__(self) -> str:
        return self.isoformat()


def _read_json(_value: Any) -> _ShimDataFrame:
    return _ShimDataFrame()


def install_lightweight_pandas_shim() -> bool:
    if "pandas" in sys.modules:
        return False

    shim = types.ModuleType("pandas")
    shim.__dict__.update(
        {
            "__doc__": "Lightweight pandas shim for Nautilus paper-mode startup.",
            "__package__": "pandas",
            "__version__": "0.0-shim",
            "DataFrame": _ShimDataFrame,
            "Timestamp": _ShimTimestamp,
            "Timedelta": _ShimTimedelta,
            "read_json": _read_json,
        },
    )
    sys.modules["pandas"] = shim
    return True
