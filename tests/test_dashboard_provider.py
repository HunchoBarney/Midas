from cbcl_platform.dashboard_provider import DashboardProvider
from cbcl_platform.models import RuntimeMode
from cbcl_platform.nautilus.services import (
    RuntimeServices,
    register_runtime_services,
    unregister_runtime_services,
)
from cbcl_platform.runtime import build_runtime
from cbcl_platform.state_store import RuntimeStateStore


def test_dashboard_provider_prefers_live_runtime_services(tmp_path) -> None:
    runtime = build_runtime(RuntimeMode.DASHBOARD)
    state_store = RuntimeStateStore(str(tmp_path / "runtime_state.json"))
    state_store.write(
        {
            "generated_at": "2020-01-01T00:00:00+00:00",
            "status": "stale",
            "system": {"runtime_id": "runtime-1", "runtime_mode": "paper"},
        },
    )

    services = RuntimeServices(
        runtime_id="runtime-1",
        runtime=build_runtime(RuntimeMode.PAPER),
        state_store=state_store,
        registry=None,  # type: ignore[arg-type]
        bindings={},
        status="running",
    )
    register_runtime_services(services)
    try:
        provider = DashboardProvider(runtime, state_store=state_store)
        payload = provider.bootstrap()
    finally:
        unregister_runtime_services("runtime-1")

    assert payload["overview"]["mode"] == "paper"
    assert payload["system"]["paper_loop"]["status"] == "running"

