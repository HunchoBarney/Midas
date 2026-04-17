from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from cbcl_platform.state_store import RuntimeStateStore

if TYPE_CHECKING:
    from cbcl_platform.runtime import TradingRuntime


def _asset_bytes(name: str) -> bytes:
    asset_dir = files("cbcl_platform.dashboard_assets")
    return asset_dir.joinpath(name).read_bytes()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def bootstrap_payload(
    *,
    runtime: TradingRuntime,
    active_state: dict[str, Any] | None,
) -> dict[str, Any]:
    from cbcl_platform.dashboard_state import DashboardState

    if active_state is not None:
        return DashboardState(runtime, active_state=active_state).bootstrap()
    from cbcl_platform.dashboard_provider import DashboardProvider

    return DashboardProvider(runtime).bootstrap()


@dataclass
class DashboardServerHandle:
    server: ThreadingHTTPServer
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2.0)
        self.server.server_close()


def _make_handler(
    *,
    runtime: TradingRuntime,
    state_store: RuntimeStateStore,
    runtime_id: str | None,
) -> type[BaseHTTPRequestHandler]:
    def _provider():
        from cbcl_platform.dashboard_provider import DashboardProvider

        return DashboardProvider(runtime, state_store=state_store, runtime_id=runtime_id)

    class Handler(BaseHTTPRequestHandler):
        def _send_bytes(
            self,
            payload: bytes,
            *,
            status: HTTPStatus = HTTPStatus.OK,
            content_type: str,
            cache_control: str = "no-store",
        ) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, payload: dict[str, Any]) -> None:
            self._send_bytes(
                _json_bytes(payload),
                content_type="application/json; charset=utf-8",
            )

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/healthz":
                self._send_bytes(b"ok", content_type="text/plain; charset=utf-8")
                return
            if path == "/snapshot.json":
                self._send_json(_provider().bootstrap())
                return
            if path == "/api/bootstrap":
                self._send_json(_provider().bootstrap())
                return
            if path == "/api/overview":
                self._send_json(_provider().overview())
                return
            if path == "/api/opportunities":
                self._send_json(_provider().opportunities())
                return
            if path == "/api/portfolio":
                self._send_json(_provider().portfolio())
                return
            if path == "/api/execution":
                self._send_json(_provider().execution())
                return
            if path == "/api/system":
                self._send_json(_provider().system())
                return
            if path == "/api/settings":
                self._send_json(_provider().settings())
                return
            if path.startswith("/assets/"):
                asset_name = path.removeprefix("/assets/")
                try:
                    payload = _asset_bytes(asset_name)
                except FileNotFoundError:
                    self.send_error(HTTPStatus.NOT_FOUND.value)
                    return
                content_type = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"
                self._send_bytes(
                    payload,
                    content_type=f"{content_type}; charset=utf-8"
                    if content_type.startswith("text/") or content_type == "application/javascript"
                    else content_type,
                    cache_control="public, max-age=60",
                )
                return

            try:
                html = _asset_bytes("index.html")
            except FileNotFoundError:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR.value)
                return
            self._send_bytes(html, content_type="text/html; charset=utf-8")

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def start_dashboard_server(
    *,
    runtime: TradingRuntime,
    host: str,
    port: int,
    runtime_id: str | None = None,
) -> DashboardServerHandle:
    state_store = RuntimeStateStore(runtime.config.runtime_state_path)
    server = ThreadingHTTPServer(
        (host, port),
        _make_handler(runtime=runtime, state_store=state_store, runtime_id=runtime_id),
    )
    thread = threading.Thread(
        target=server.serve_forever,
        name="cbcl-dashboard-server",
        daemon=True,
    )
    thread.start()
    return DashboardServerHandle(server=server, thread=thread)


def serve_dashboard(
    *,
    runtime: TradingRuntime,
    host: str,
    port: int,
    duration_seconds: float = 0.0,
    runtime_id: str | None = None,
) -> None:
    state_store = RuntimeStateStore(runtime.config.runtime_state_path)
    server = ThreadingHTTPServer(
        (host, port),
        _make_handler(runtime=runtime, state_store=state_store, runtime_id=runtime_id),
    )
    if duration_seconds > 0:
        timer = threading.Timer(duration_seconds, server.shutdown)
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
