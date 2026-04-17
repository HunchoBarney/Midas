from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass
class JsonHttpResponse:
    status_code: int
    payload: Any

    def raise_for_status(self) -> None:
        if 200 <= self.status_code < 300:
            return
        raise RuntimeError(f"HTTP request failed with status {self.status_code}")

    def json(self) -> Any:
        return self.payload


class UrlopenAsyncClient:
    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            ),
        }

    async def get(self, url: str, *, params: dict[str, Any] | None = None) -> JsonHttpResponse:
        return await asyncio.to_thread(self._get_sync, url, params)

    async def aclose(self) -> None:
        return None

    def _get_sync(
        self,
        url: str,
        params: dict[str, Any] | None,
    ) -> JsonHttpResponse:
        full_url = url
        if params:
            query = urlencode(
                {
                    key: value
                    for key, value in params.items()
                    if value is not None
                },
                doseq=True,
            )
            separator = "&" if "?" in full_url else "?"
            full_url = f"{full_url}{separator}{query}"
        request = Request(full_url, headers=self._headers)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return JsonHttpResponse(status_code=int(response.status), payload=payload)
        except HTTPError as exc:
            payload: Any
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                payload = {"error": str(exc)}
            return JsonHttpResponse(status_code=int(exc.code), payload=payload)
