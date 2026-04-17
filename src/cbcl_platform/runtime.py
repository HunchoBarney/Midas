from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from cbcl_platform.config import RuntimeConfig
from cbcl_platform.execution import ExecutionCore
from cbcl_platform.kelly import KellySizingEngine
from cbcl_platform.models import DashboardSnapshot, RuntimeMode
from cbcl_platform.paper import RealisticPaperExecutionAdapter
from cbcl_platform.strategy import CbClDivergenceStrategy


@dataclass
class TradingRuntime:
    mode: RuntimeMode
    config: RuntimeConfig
    strategy: CbClDivergenceStrategy
    kelly: KellySizingEngine
    execution_core: ExecutionCore
    paper_execution: RealisticPaperExecutionAdapter | None

    def _actual_data_stack(self) -> list[str]:
        if self.mode in {RuntimeMode.PAPER, RuntimeMode.LIVE}:
            return [
                "nautilus_polymarket_gamma_discovery",
                "nautilus_polymarket_market_ws",
                "nautilus_coinbase_spot_ws",
                "nautilus_rtds_chainlink_ws",
            ]
        return []

    def _planned_data_stack(self) -> list[str]:
        return [
            "official_nautilus_polymarket",
            "custom_coinbase_spot",
            "custom_polymarket_rtds",
        ]

    def _actual_execution_stack(self) -> list[str]:
        if self.mode == RuntimeMode.PAPER:
            return ["nautilus_polymarket_paper"]
        if self.mode == RuntimeMode.LIVE:
            return ["nautilus_polymarket_live"]
        return []

    def _planned_execution_stack(self) -> list[str]:
        return ["official_nautilus_polymarket_live"]

    def summary(self) -> dict[str, Any]:
        paper_cfg = self.config.paper_execution
        profile = self.strategy.profile_summary()
        return {
            "mode": self.mode.value,
            "environment": self.config.environment,
            "markets": list(self.config.markets),
            "strategy_name": self.config.strategy.strategy_name,
            "threshold": self.config.strategy.threshold,
            "max_minutes_to_close_5m": self.config.strategy.max_minutes_to_close_5m,
            "max_minutes_to_close_15m": self.config.strategy.max_minutes_to_close_15m,
            "min_buy_price": self.config.strategy.min_buy_price,
            "hard_cap": self.config.strategy.max_buy_price,
            "max_price_drift": self.config.strategy.max_price_drift,
            "signal_profile": profile,
            "realistic_paper_enabled": self.mode == RuntimeMode.PAPER,
            "kelly_enabled": self.config.kelly.enabled,
            "kelly_fraction": self.config.kelly.fraction,
            "kelly_max_bankroll_fraction": self.config.kelly.max_bankroll_fraction,
            "kelly_bootstrap_enable_balance_usd": self.config.kelly.bootstrap_enable_balance_usd,
            "kelly_bootstrap_disable_balance_usd": self.config.kelly.bootstrap_disable_balance_usd,
            "paper_delay_model": {
                "internal": asdict(paper_cfg.internal_delay),
                "signing": asdict(paper_cfg.signing_delay),
                "submit_rtt": asdict(paper_cfg.submit_rtt),
                "ack_delay": asdict(paper_cfg.ack_delay),
                "confirm_min_ms": paper_cfg.matched_to_confirmed_min_ms,
                "confirm_max_ms": paper_cfg.matched_to_confirmed_max_ms,
            },
            "data_stack": self._actual_data_stack(),
            "planned_data_stack": self._planned_data_stack(),
            "execution_stack": self._actual_execution_stack(),
            "planned_execution_stack": self._planned_execution_stack(),
        }

    def dashboard_snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            runtime_mode=self.mode.value,
            environment=self.config.environment,
            markets=self.config.markets,
            strategy_name=self.config.strategy.strategy_name,
            hard_cap=self.config.strategy.max_buy_price,
            max_drift=self.config.strategy.max_price_drift,
            realistic_paper_enabled=self.mode == RuntimeMode.PAPER,
            kelly_enabled=self.config.kelly.enabled,
            commands=(
                "start-live",
                "start-paper",
                "start-dashboard",
                "run-replay",
                "run-backtest",
            ),
            notes=(
                "Hot path stays in-process: Polymarket, Coinbase, RTDS, strategy, Kelly, "
                "execution.",
                "Paper mode always uses the realistic execution model.",
                "Live execution starts on the official Nautilus Polymarket adapter, "
                "but is intentionally swappable.",
            ),
            metadata=self.summary(),
        )


def build_runtime(mode: RuntimeMode, config: RuntimeConfig | None = None) -> TradingRuntime:
    config = config or RuntimeConfig.from_env()
    kelly = KellySizingEngine(config=config.kelly)
    strategy = CbClDivergenceStrategy(
        config=config.strategy,
        freshness=config.freshness,
        kelly=kelly,
    )
    execution_core = ExecutionCore()
    paper_execution = None
    if mode == RuntimeMode.PAPER:
        paper_execution = RealisticPaperExecutionAdapter(
            execution_core=execution_core,
            config=config.paper_execution,
        )
    return TradingRuntime(
        mode=mode,
        config=config,
        strategy=strategy,
        kelly=kelly,
        execution_core=execution_core,
        paper_execution=paper_execution,
    )
