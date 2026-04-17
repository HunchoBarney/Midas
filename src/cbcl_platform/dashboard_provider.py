from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cbcl_platform.state_store import RuntimeStateStore

if TYPE_CHECKING:
    from cbcl_platform.runtime import TradingRuntime


class DashboardProvider:
    def __init__(
        self,
        runtime: TradingRuntime,
        *,
        state_store: RuntimeStateStore | None = None,
        runtime_id: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._state_store = state_store or RuntimeStateStore(runtime.config.runtime_state_path)
        self._runtime_id = runtime_id

    def active_state(self) -> dict[str, Any] | None:
        from cbcl_platform.nautilus.services import lookup_runtime_services

        services = lookup_runtime_services(self._runtime_id)
        if services is not None:
            payload = dict(services.snapshot_payload())
            payload["provider_source"] = "in_process"
            return payload

        persisted = self._state_store.read()
        if persisted is None:
            return None

        runtime_id = (
            self._runtime_id
            or str(persisted.get("system", {}).get("runtime_id") or "")
            or None
        )
        live_services = lookup_runtime_services(runtime_id)
        if live_services is not None:
            payload = dict(live_services.snapshot_payload())
            payload["provider_source"] = "in_process"
            return payload
        payload = dict(persisted)
        payload["provider_source"] = "persisted_fallback"
        return payload

    def state(self) -> DashboardState:
        from cbcl_platform.dashboard_state import DashboardState

        return DashboardState(self._runtime, active_state=self.active_state())

    def bootstrap(self) -> dict[str, Any]:
        return self.state().bootstrap()

    def overview(self) -> dict[str, Any]:
        return self.state().overview()

    def opportunities(self) -> dict[str, Any]:
        return self.state().opportunities()

    def portfolio(self) -> dict[str, Any]:
        return self.state().portfolio()

    def execution(self) -> dict[str, Any]:
        return self.state().execution()

    def system(self) -> dict[str, Any]:
        return self.state().system()

    def settings(self) -> dict[str, Any]:
        return self.state().settings()
