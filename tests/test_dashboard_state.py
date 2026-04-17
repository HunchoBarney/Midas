from cbcl_platform.dashboard_state import DashboardState
from cbcl_platform.models import RuntimeMode
from cbcl_platform.runtime import build_runtime


def test_dashboard_state_exposes_bootstrap_sections() -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    payload = DashboardState(runtime).bootstrap()

    assert payload["overview"]["mode"] == "paper"
    assert payload["opportunities"]["status"] == "blocked"
    assert payload["opportunities"]["monitor_rows"] == []
    assert payload["portfolio"]["summary"]["cash_balance_usd"] == 1000.0
    assert payload["execution"]["policy"]["order_type"] == "IOC aggressive limit"
    assert [item["id"] for item in payload["navigation"]] == [
        "overview",
        "execution",
        "portfolio",
        "system",
    ]


def test_dashboard_state_surfaces_delay_model_and_component_status() -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    payload = DashboardState(runtime).bootstrap()

    assert payload["execution"]["delay_model"]["signing_ms"]["p50_ms"] == 1000
    components = {item["component"]: item["status"] for item in payload["system"]["components"]}
    assert components["Strategy engine"] == "implemented"
    assert components["Market registry"] == "planned"


def test_dashboard_state_uses_active_runtime_state_when_available() -> None:
    runtime = build_runtime(RuntimeMode.DASHBOARD)
    payload = DashboardState(
        runtime,
        active_state={
            "generated_at": "2026-04-14T20:00:00+00:00",
            "status": "running",
            "loop": {"avg_tick_ms": 3.2, "max_tick_ms": 7.9},
            "metrics": {"orders_submitted": 4, "fills": 3, "signals_seen": 12},
            "opportunities": {
                "status": "live",
                "summary": "Live rows",
                "columns": ["coin", "market", "signal_state"],
                "rows": [{"coin": "BTC", "market": "BTC 5m", "signal_state": "READY"}],
                "monitor_rows": [
                    {
                        "coin": "BTC",
                        "active_market": "no active updown",
                        "spot_price": 74250.11,
                        "oracle_price": 74200.11,
                        "divergence_pct": 0.0674,
                        "spot_move_1m_pct": 0.12,
                        "oracle_move_1m_pct": 0.08,
                        "volume_24h": 18234.55,
                        "feed_skew_ms": 122.0,
                        "freshness": "cb 20ms | cl 31ms",
                        "market_state": "watching live feeds",
                    }
                ],
            },
            "portfolio": {
                "cash_balance_usd": 978.4,
                "total_exposure_usd": 21.6,
                "realized_pnl_usd": 1.2,
                "open_positions": 1,
                "positions": [
                    {
                        "market_id": "btc-5m-0001",
                        "yes_shares": 5.0,
                        "no_shares": 0.0,
                        "cost_basis_usd": 21.6,
                        "fees_paid_usd": 0.0,
                    }
                ],
                "mode_note": "Active paper runtime",
            },
            "execution": {
                "orders": [{"market": "BTC 5m", "status": "filled"}],
                "fills": [{"market": "BTC 5m", "status": "filled"}],
                "rejects": [],
                "settlements": [],
            },
            "system": {
                "feed_mode": "simulated",
                "state_path": "/tmp/runtime_state.json",
                "latency_mode": "submit-time book evaluation with modeled delays",
                "runtime_mode": "paper",
            },
        },
    ).bootstrap()

    assert payload["opportunities"]["status"] == "live"
    assert payload["portfolio"]["summary"]["open_positions"] == 1
    assert payload["execution"]["orders"][0]["market"] == "BTC 5m"
    assert payload["system"]["paper_loop"]["status"] == "running"
    assert payload["overview"]["mode"] == "paper"
    assert payload["opportunities"]["monitor_rows"][0]["coin"] == "BTC"
    assert payload["opportunities"]["monitor_rows"][0]["volume_24h"] == 18234.55
    assert payload["generated_at"] == "2026-04-14T20:00:00+00:00"


def test_dashboard_state_flags_stale_runtime_state() -> None:
    runtime = build_runtime(RuntimeMode.DASHBOARD)
    payload = DashboardState(
        runtime,
        active_state={
            "generated_at": "2020-01-01T00:00:00+00:00",
            "status": "running",
            "system": {"runtime_mode": "paper", "feed_mode": "simulated"},
        },
    ).bootstrap()

    assert payload["overview"]["state_is_stale"] is True
    assert any(
        alert["title"] == "Runtime state is stale"
        for alert in payload["overview"]["alerts"]
    )
