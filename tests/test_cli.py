from cbcl_platform import __version__
from cbcl_platform.cli import main
from cbcl_platform.config import RuntimeConfig


def test_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_runtime_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("CBCL_ENV", raising=False)
    monkeypatch.delenv("CBCL_MARKETS", raising=False)
    config = RuntimeConfig.from_env()
    assert config.environment == "dev"
    assert config.markets == ("BTC", "ETH")
    assert config.strategy.max_minutes_to_close_5m == 2.0
    assert config.strategy.max_minutes_to_close_15m == 2.0
    assert config.paper_execution.market_duration_5m_s == 300
    assert config.paper_execution.market_duration_15m_s == 900


def test_cli_modes_return_success() -> None:
    assert main(["start-paper", "--json"]) == 0
    assert main(["start-live", "--json"]) == 0
    assert main(["run-backtest", "--json"]) == 0
    assert main(["run-replay", "--json"]) == 0


def test_start_paper_runs_bounded_loop_and_writes_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CBCL_RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"))
    monkeypatch.setenv("CBCL_PAPER_LOOP_INTERVAL_MS", "25")
    monkeypatch.setenv("CBCL_PAPER_STATE_FLUSH_INTERVAL_MS", "25")
    runtime_path = tmp_path / "runtime_state.json"

    async def fake_run_trading_node(
        *,
        mode,
        duration_seconds: float = 0.0,
        with_dashboard: bool = False,
    ) -> int:
        runtime_path.write_text(
            __import__("json").dumps(
                {
                    "generated_at": "2026-04-15T00:00:00+00:00",
                    "status": "stopped",
                    "system": {"runtime_mode": mode.value, "state_path": str(runtime_path)},
                }
            )
        )
        assert mode.value == "paper"
        assert duration_seconds == 0.35
        assert with_dashboard is False
        return 0

    monkeypatch.setattr("cbcl_platform.cli.run_trading_node", fake_run_trading_node)

    assert main(["start-paper", "--duration-seconds", "0.35"]) == 0
    assert runtime_path.exists()


def test_start_paper_can_embed_dashboard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CBCL_RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"))
    runtime_path = tmp_path / "runtime_state.json"

    async def fake_run_trading_node(
        *,
        mode,
        duration_seconds: float = 0.0,
        with_dashboard: bool = False,
    ) -> int:
        runtime_path.write_text(
            __import__("json").dumps(
                {
                    "generated_at": "2026-04-15T00:00:00+00:00",
                    "status": "stopped",
                    "system": {"runtime_mode": mode.value, "state_path": str(runtime_path)},
                }
            )
        )
        assert mode.value == "paper"
        assert duration_seconds == 0.35
        assert with_dashboard is True
        return 0

    monkeypatch.setattr("cbcl_platform.cli.run_trading_node", fake_run_trading_node)

    assert main(["start-paper", "--duration-seconds", "0.35", "--with-dashboard"]) == 0
    assert runtime_path.exists()


def test_start_live_runs_bounded_node(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CBCL_RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"))
    runtime_path = tmp_path / "runtime_state.json"

    async def fake_run_trading_node(
        *,
        mode,
        duration_seconds: float = 0.0,
        with_dashboard: bool = False,
    ) -> int:
        runtime_path.write_text(
            __import__("json").dumps(
                {
                    "generated_at": "2026-04-15T00:00:00+00:00",
                    "status": "stopped",
                    "system": {"runtime_mode": mode.value, "state_path": str(runtime_path)},
                }
            )
        )
        assert mode.value == "live"
        assert duration_seconds == 0.2
        assert with_dashboard is False
        return 0

    monkeypatch.setattr("cbcl_platform.cli.run_trading_node", fake_run_trading_node)
    monkeypatch.setenv("POLYMARKET_PK", "pk")
    monkeypatch.setenv("POLYMARKET_FUNDER", "funder")
    monkeypatch.setenv("POLYMARKET_API_KEY", "key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "secret")
    monkeypatch.setenv("POLYMARKET_PASSPHRASE", "pass")

    assert main(["start-live", "--duration-seconds", "0.2"]) == 0
    assert runtime_path.exists()


def test_start_live_can_embed_dashboard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CBCL_RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"))
    runtime_path = tmp_path / "runtime_state.json"

    async def fake_run_trading_node(
        *,
        mode,
        duration_seconds: float = 0.0,
        with_dashboard: bool = False,
    ) -> int:
        runtime_path.write_text(
            __import__("json").dumps(
                {
                    "generated_at": "2026-04-15T00:00:00+00:00",
                    "status": "stopped",
                    "system": {"runtime_mode": mode.value, "state_path": str(runtime_path)},
                }
            )
        )
        assert mode.value == "live"
        assert duration_seconds == 0.2
        assert with_dashboard is True
        return 0

    monkeypatch.setattr("cbcl_platform.cli.run_trading_node", fake_run_trading_node)
    monkeypatch.setenv("POLYMARKET_PK", "pk")
    monkeypatch.setenv("POLYMARKET_FUNDER", "funder")
    monkeypatch.setenv("POLYMARKET_API_KEY", "key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "secret")
    monkeypatch.setenv("POLYMARKET_PASSPHRASE", "pass")

    assert main(["start-live", "--duration-seconds", "0.2", "--with-dashboard"]) == 0
    assert runtime_path.exists()
