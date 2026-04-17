from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cbcl_platform.models import RuntimeMode

if TYPE_CHECKING:
    from cbcl_platform.runtime import TradingRuntime


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _status_tone(status: str) -> str:
    return {
        "live": "warning",
        "paper": "good",
        "implemented": "good",
        "available": "good",
        "planned": "warning",
        "blocked": "warning",
        "not_started": "danger",
    }.get(str(status).lower(), "neutral")


class DashboardState:
    def __init__(
        self,
        runtime: TradingRuntime,
        *,
        active_state: dict[str, Any] | None = None,
    ) -> None:
        self._runtime = runtime
        self._active_state = active_state or {}

    def _active(self, key: str) -> dict[str, Any]:
        payload = self._active_state.get(key)
        return payload if isinstance(payload, dict) else {}

    def _active_list(self, key: str, child: str) -> list[dict[str, Any]]:
        payload = self._active(key).get(child)
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _active_generated_at(self) -> str | None:
        generated_at = self._active_state.get("generated_at")
        return str(generated_at) if generated_at else None

    def _provider_source(self) -> str:
        source = self._active_state.get("provider_source")
        return str(source) if source else "unknown"

    def _state_age_ms(self) -> float | None:
        parsed = _parse_iso(self._active_generated_at())
        if parsed is None:
            return None
        return max(0.0, (datetime.now(UTC) - parsed).total_seconds() * 1000.0)

    def _state_is_stale(self) -> bool:
        age_ms = self._state_age_ms()
        if age_ms is None:
            return False
        stale_after_ms = max(
            2_000,
            self._runtime.config.paper_execution.state_flush_interval_ms * 3,
        )
        return age_ms > stale_after_ms

    def _display_status(self) -> str:
        raw_status = str(self._active_state.get("status") or "idle").lower()
        if self._provider_source() != "in_process" and self._state_is_stale():
            return "stale"
        return raw_status

    def _display_generated_at(self) -> str:
        return self._active_generated_at() or _now_iso()

    def _display_runtime_mode(self) -> str:
        active_mode = self._active("system").get("runtime_mode")
        if active_mode:
            return str(active_mode)
        return self._runtime.mode.value

    def _display_data_stack(self) -> list[str]:
        active_system = self._active("system")
        if isinstance(active_system.get("data_stack"), list):
            return [str(item) for item in active_system["data_stack"]]
        runtime_mode = self._display_runtime_mode()
        feed_mode = str(active_system.get("feed_mode") or "").lower()
        if runtime_mode == RuntimeMode.PAPER.value and feed_mode == "simulated":
            return [
                "simulated_coinbase_feed",
                "simulated_chainlink_feed",
                "simulated_polymarket_books",
            ]
        return list(self._runtime.summary()["data_stack"])

    def _display_execution_stack(self) -> list[str]:
        active_system = self._active("system")
        if isinstance(active_system.get("execution_stack"), list):
            return [str(item) for item in active_system["execution_stack"]]
        runtime_mode = self._display_runtime_mode()
        if runtime_mode == RuntimeMode.PAPER.value:
            return ["realistic_polymarket_paper"]
        return list(self._runtime.summary()["execution_stack"])

    def _portfolio_snapshot(self) -> dict[str, Any]:
        if self._runtime.paper_execution is None:
            return {
                "cash_balance_usd": 0.0,
                "total_exposure_usd": 0.0,
                "realized_pnl_usd": 0.0,
                "open_positions": 0,
                "positions": [],
                "mode_note": (
                    "Live portfolio telemetry will appear when the live execution adapter is wired."
                ),
            }

        portfolio = self._runtime.paper_execution.portfolio
        snapshot = portfolio.snapshot()
        positions = []
        for market_id, position in sorted(portfolio.positions.items()):
            positions.append(
                {
                    "market_id": market_id,
                    "yes_shares": round(position.yes_shares, 4),
                    "no_shares": round(position.no_shares, 4),
                    "cost_basis_usd": round(position.cost_basis_usd, 4),
                    "fees_paid_usd": round(position.fees_paid_usd, 4),
                }
            )
        return {
            "cash_balance_usd": round(snapshot.cash_balance_usd, 4),
            "total_exposure_usd": round(snapshot.total_exposure_usd, 4),
            "realized_pnl_usd": round(portfolio.realized_pnl_usd, 4),
            "open_positions": snapshot.open_positions,
            "positions": positions,
            "mode_note": "Portfolio reflects the in-process realistic paper execution ledger.",
        }

    def _portfolio_data(self) -> dict[str, Any]:
        active_portfolio = self._active("portfolio")
        if active_portfolio:
            return {
                "cash_balance_usd": float(active_portfolio.get("cash_balance_usd", 0.0)),
                "total_exposure_usd": float(active_portfolio.get("total_exposure_usd", 0.0)),
                "realized_pnl_usd": float(active_portfolio.get("realized_pnl_usd", 0.0)),
                "open_positions": int(active_portfolio.get("open_positions", 0)),
                "positions": list(active_portfolio.get("positions") or []),
                "mode_note": str(
                    active_portfolio.get(
                        "mode_note",
                        "Portfolio reflects the active paper runtime.",
                    )
                ),
                "empty_state": str(
                    active_portfolio.get(
                        "empty_state",
                        "No positions are open yet.",
                    )
                ),
            }
        return self._portfolio_snapshot()

    def _component_status(self) -> list[dict[str, Any]]:
        runtime = self._runtime
        kelly_status = "implemented" if runtime.config.kelly.enabled else "available"
        paper_status = (
            "implemented"
            if runtime.paper_execution or self._display_runtime_mode() == RuntimeMode.PAPER.value
            else "available"
        )
        active_system = self._active("system")
        active_loop = self._active("loop")
        active_runtime = self._display_status()
        components = [
            {
                "component": "Paper trading loop",
                "status": (
                    "implemented"
                    if active_runtime == "running"
                    else "available"
                    if active_runtime == "starting"
                    else "planned"
                ),
                "tone": _status_tone(
                    "implemented"
                    if active_runtime == "running"
                    else "available"
                    if active_runtime == "starting"
                    else "planned"
                ),
                "detail": (
                    f"Tick avg {active_loop.get('avg_tick_ms', 0.0)}ms | "
                    f"max {active_loop.get('max_tick_ms', 0.0)}ms | "
                    f"state writer {runtime.config.paper_execution.state_flush_interval_ms}ms."
                    if active_runtime == "running"
                    else (
                        "Paper runtime is booting. Nautilus is still initializing feeds, "
                        "clients, and strategy state."
                        if active_runtime == "starting"
                        else "No active paper process is publishing runtime state yet."
                    )
                ),
            },
            {
                "component": "Strategy engine",
                "status": "implemented",
                "tone": _status_tone("implemented"),
                "detail": f"{runtime.config.strategy.strategy_name} is active in local code.",
            },
            {
                "component": "Kelly sizing",
                "status": kelly_status,
                "tone": _status_tone(kelly_status),
                "detail": (
                    "Bootstrap hysteresis and fractional Kelly are wired into the strategy path."
                ),
            },
            {
                "component": "Execution core",
                "status": "implemented",
                "tone": _status_tone("implemented"),
                "detail": (
                    "Adaptive book walk, hard-cap enforcement, and drift rejection are implemented."
                ),
            },
            {
                "component": "Realistic paper execution",
                "status": paper_status,
                "tone": _status_tone(paper_status),
                "detail": (
                    "Submit-time book evaluation, modeled signing/network delays, "
                    "and resolution settlement."
                ),
            },
            {
                "component": "Polymarket live adapter",
                "status": "implemented" if active_system.get("feed_mode") == "live" else "planned",
                "tone": _status_tone(
                    "implemented" if active_system.get("feed_mode") == "live" else "planned"
                ),
                "detail": (
                    "Nautilus Polymarket discovery and market websocket feeds are active."
                    if active_system.get("feed_mode") == "live"
                    else "Official Nautilus Polymarket data/execution wiring is still pending."
                ),
            },
            {
                "component": "Coinbase spot client",
                "status": "implemented" if active_system.get("feed_mode") == "live" else "planned",
                "tone": _status_tone(
                    "implemented" if active_system.get("feed_mode") == "live" else "planned"
                ),
                "detail": (
                    "Live Coinbase spot prices are feeding signal generation."
                    if active_system.get("feed_mode") == "live"
                    else (
                        "Required before opportunity ranking and market-level signal "
                        "monitoring can go live."
                    )
                ),
            },
            {
                "component": "Polymarket RTDS client",
                "status": "implemented" if active_system.get("feed_mode") == "live" else "planned",
                "tone": _status_tone(
                    "implemented" if active_system.get("feed_mode") == "live" else "planned"
                ),
                "detail": (
                    "Live RTDS Chainlink prices are feeding divergence checks."
                    if active_system.get("feed_mode") == "live"
                    else "Required for Chainlink-side divergence inputs and feed freshness status."
                ),
            },
            {
                "component": "Market registry",
                "status": "implemented" if active_system.get("feed_mode") == "live" else "planned",
                "tone": _status_tone(
                    "implemented" if active_system.get("feed_mode") == "live" else "planned"
                ),
                "detail": (
                    "Rolling BTC/ETH 5m/15m market discovery is active."
                    if active_system.get("feed_mode") == "live"
                    else "Rolling 5m/15m market discovery is not wired into this runtime yet."
                ),
            },
            {
                "component": "Dashboard API",
                "status": "implemented",
                "tone": _status_tone("implemented"),
                "detail": "Structured dashboard view models are now served from the Python app.",
            },
        ]
        return components

    def _alerts(self, portfolio: dict[str, Any]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        if self._runtime.mode == RuntimeMode.PAPER:
            alerts.append(
                {
                    "level": "good",
                    "title": "Realistic paper mode active",
                    "detail": (
                        "Execution timing is modeled after decision time, using "
                        "submit-time book checks."
                    ),
                }
            )
        if self._state_is_stale():
            age_ms = self._state_age_ms() or 0.0
            alerts.append(
                {
                    "level": "warning",
                    "title": "Runtime state is stale",
                    "detail": (
                        f"Last bot update was {age_ms / 1000.0:.1f}s ago. The dashboard is showing "
                        "the last persisted snapshot, not a live feed."
                    ),
                }
            )
        active_runtime = self._display_status()
        active_metrics = self._active("metrics")
        active_system = self._active("system")
        active_opportunities = self._active("opportunities")
        if active_runtime == "running":
            alerts.append(
                {
                    "level": "good",
                    "title": "Paper bot is running",
                    "detail": (
                        f"Signals {active_metrics.get('signals_seen', 0)} | "
                        f"orders {active_metrics.get('orders_submitted', 0)} | "
                        f"fills {active_metrics.get('fills', 0)} | "
                        f"rejections {active_metrics.get('rejections', 0)}."
                    ),
                }
            )
        elif active_runtime == "starting":
            alerts.append(
                {
                    "level": "warning",
                    "title": "Paper bot is still booting",
                    "detail": (
                        "The dashboard server is live, but Nautilus is still initializing feeds, "
                        "clients, and strategy state."
                    ),
                }
            )
        elif active_runtime == "stale":
            alerts.append(
                {
                    "level": "warning",
                    "title": "No live bot process attached",
                    "detail": (
                        "The dashboard is showing the last persisted snapshot because there is "
                        "no in-process runtime currently connected."
                    ),
                }
            )
        if portfolio["open_positions"] == 0 and active_runtime not in {"running", "starting"}:
            live_feed_mode = str(active_system.get("feed_mode") or "").lower() == "live"
            has_rows = bool(active_opportunities.get("rows"))
            alerts.append(
                {
                    "level": "warning",
                    "title": (
                        "No tradable BTC/ETH contract in window"
                        if live_feed_mode and not has_rows
                        else "No live market state connected"
                    ),
                    "detail": (
                        (
                            "Live feeds are connected, but there is no currently tradable "
                            "BTC/ETH 5m/15m UpDown contract in the active board. "
                            "Use the top market monitor for BTC/ETH movement, volume, "
                            "divergence, skew, and freshness while the bot waits."
                        )
                        if live_feed_mode and not has_rows
                        else (
                            "Opportunities, positions, and execution feeds remain "
                            "empty until the market registry and live data adapters are wired."
                        )
                    ),
                }
            )
        alerts.append(
            {
                "level": "neutral",
                "title": "Dashboard is read-only",
                "detail": (
                    "Safe control surfaces and live operator actions are reserved for a later pass."
                ),
            }
        )
        return alerts

    def overview(self) -> dict[str, Any]:
        runtime = self._runtime
        portfolio = self._portfolio_data()
        free_capital = float(portfolio["cash_balance_usd"])
        deployed = float(portfolio["total_exposure_usd"])
        equity = free_capital + deployed
        strategy = runtime.config.strategy
        signal_profile = runtime.strategy.profile_summary()
        kelly = runtime.config.kelly
        active_metrics = self._active("metrics")
        orders_submitted = int(active_metrics.get("orders_submitted", 0))
        realized_pnl = float(portfolio["realized_pnl_usd"])
        settled_count = int(active_metrics.get("settlements", 0))
        win_rate_pct = float(active_metrics.get("win_rate_pct", 0.0))
        fill_rate = (
            (float(active_metrics.get("fills", 0)) / float(max(1, orders_submitted))) * 100.0
            if orders_submitted > 0
            else 0.0
        )
        return {
            "generated_at": self._display_generated_at(),
            "state_age_ms": round(self._state_age_ms() or 0.0, 1),
            "state_is_stale": self._state_is_stale(),
            "mode": self._display_runtime_mode(),
            "environment": runtime.config.environment,
            "markets": list(runtime.config.markets),
            "headline": {
                "title": "CBCL operator workspace",
                "subtitle": (
                    "Premium monitoring shell for the cb_cl_005 paper/live runtime. "
                    "Trader-critical sections are ready to consume live state once "
                    "adapters are wired."
                ),
            },
            "hero": {
                "primary_label": "Runtime mode",
                "primary_value": self._display_runtime_mode().upper(),
                "secondary_label": "Strategy",
                "secondary_value": strategy.strategy_name,
                "summary": (
                    f"Hard cap {strategy.max_buy_price:.2f} | "
                    f"drift {strategy.max_price_drift:.2f} | "
                    f"signal window {float(signal_profile['signal_max_minutes_to_close_5m']):.1f}m"
                ),
            },
            "metrics": [
                {
                    "label": "Equity",
                    "value": _fmt_money(equity),
                    "tone": "neutral",
                    "detail": "Cash plus deployed exposure.",
                },
                {
                    "label": "Free capital",
                    "value": _fmt_money(free_capital),
                    "tone": "good",
                    "detail": "Immediately available bankroll.",
                },
                {
                    "label": "Deployed",
                    "value": _fmt_money(deployed),
                    "tone": "warning" if deployed > 0 else "neutral",
                    "detail": "Current exposure committed to positions.",
                },
                {
                    "label": "Open positions",
                    "value": str(portfolio["open_positions"]),
                    "tone": "warning" if portfolio["open_positions"] else "neutral",
                    "detail": "Unsettled paper positions.",
                },
                {
                    "label": "Realized PnL",
                    "value": _fmt_money(realized_pnl),
                    "tone": (
                        "good" if realized_pnl > 0 else "danger" if realized_pnl < 0 else "neutral"
                    ),
                    "detail": "Settled PnL only. Open-position mark-to-market is not modeled yet.",
                },
                {
                    "label": "Win rate",
                    "value": f"{win_rate_pct:.1f}%",
                    "tone": (
                        "good"
                        if settled_count > 0 and win_rate_pct >= 50.0
                        else "warning"
                        if settled_count > 0
                        else "neutral"
                    ),
                    "detail": (
                        f"Settled markets only. {settled_count} resolved "
                        "("
                        f"{int(active_metrics.get('wins', 0))}W / "
                        f"{int(active_metrics.get('losses', 0))}L"
                        ")."
                    ),
                },
                {
                    "label": "Orders",
                    "value": str(orders_submitted),
                    "tone": "good" if orders_submitted > 0 else "neutral",
                    "detail": "Total paper order attempts in the active runtime.",
                },
                {
                    "label": "Fill rate",
                    "value": f"{fill_rate:.1f}%",
                    "tone": (
                        "good" if fill_rate >= 50.0 else "warning" if fill_rate > 0.0 else "neutral"
                    ),
                    "detail": (
                        "Filled or partial orders divided by submitted orders. "
                        f"Kelly {'enabled' if kelly.enabled else 'disabled'}."
                    ),
                },
            ],
            "alerts": self._alerts(portfolio),
        }

    def opportunities(self) -> dict[str, Any]:
        active_opportunities = self._active("opportunities")
        if active_opportunities:
            notes = [
                (
                    "Rows come from the active paper bot and are ranked by readiness "
                    "then divergence."
                ),
                (
                    "Executable price uses the same adaptive in-memory book walk as "
                    "the execution path."
                ),
                "This uses live venue feeds when the paper runtime is running.",
            ]
            active_notes = active_opportunities.get("notes")
            if isinstance(active_notes, list):
                notes = [str(item) for item in active_notes if item]
            return {
                "status": active_opportunities.get("status", "live"),
                "summary": active_opportunities.get(
                    "summary",
                    "Live opportunity rows from the active paper runtime.",
                ),
                "monitor_summary": active_opportunities.get(
                    "monitor_summary",
                    (
                        "Live BTC/ETH feed monitor stays populated even when no tradable "
                        "UpDown contract is currently bound."
                    ),
                ),
                "monitor_columns": list(active_opportunities.get("monitor_columns") or []),
                "monitor_rows": list(active_opportunities.get("monitor_rows") or []),
                "columns": list(active_opportunities.get("columns") or []),
                "rows": list(active_opportunities.get("rows") or []),
                "notes": notes,
            }
        return {
            "status": "blocked",
            "summary": (
                "Opportunity ranking is intentionally empty until live market state is available."
            ),
            "monitor_summary": (
                "Market monitor rows appear once the runtime is publishing live BTC/ETH feeds."
            ),
            "monitor_columns": [
                "coin",
                "active_market",
                "spot_price",
                "oracle_price",
                "divergence_pct",
                "spot_move_1m_pct",
                "oracle_move_1m_pct",
                "volume_24h",
                "feed_skew_ms",
                "freshness",
                "market_state",
            ],
            "monitor_rows": [],
            "columns": [
                "coin",
                "market",
                "side",
                "minutes_to_close",
                "divergence_pct",
                "best_ask",
                "exec_price",
                "depth_under_cap",
                "freshness",
                "signal_state",
            ],
            "rows": [],
            "notes": [
                "Needs rolling market registry for active 5m/15m contracts.",
                "Needs Coinbase spot and Polymarket RTDS ingestion.",
                "Needs live Polymarket order book cache to compute executable prices.",
            ],
        }

    def portfolio(self) -> dict[str, Any]:
        portfolio = self._portfolio_data()
        allocations = []
        total_exposure = max(0.0, float(portfolio["total_exposure_usd"]))
        for position in portfolio["positions"]:
            cost_basis = float(position["cost_basis_usd"])
            share = (cost_basis / total_exposure * 100.0) if total_exposure > 0 else 0.0
            allocations.append(
                {
                    "market_id": position["market_id"],
                    "share_pct": round(share, 2),
                    "cost_basis_usd": position["cost_basis_usd"],
                }
            )
        return {
            "summary": {
                "cash_balance_usd": portfolio["cash_balance_usd"],
                "total_exposure_usd": portfolio["total_exposure_usd"],
                "realized_pnl_usd": portfolio["realized_pnl_usd"],
                "open_positions": portfolio["open_positions"],
                "mode_note": portfolio["mode_note"],
            },
            "positions": portfolio["positions"],
            "allocations": allocations,
            "empty_state": (
                "No positions are open yet. Once the realistic paper executor starts trading, "
                "positions, exposure, and settlement inventory will appear here."
            ),
        }

    def execution(self) -> dict[str, Any]:
        paper_cfg = self._runtime.config.paper_execution
        active_execution = self._active("execution")
        orders = self._active_list("execution", "orders")
        fills = self._active_list("execution", "fills")
        rejects = self._active_list("execution", "rejects")
        settlements = self._active_list("execution", "settlements")
        return {
            "policy": {
                "order_type": "IOC aggressive limit",
                "partial_fills_allowed": self._runtime.execution_core.allow_partial_fills,
                "hard_cap": self._runtime.config.strategy.max_buy_price,
                "max_price_drift": self._runtime.config.strategy.max_price_drift,
                "decision_rule": "single submit path using adaptive in-memory book walk",
            },
            "delay_model": {
                "internal_ms": asdict(paper_cfg.internal_delay),
                "signing_ms": asdict(paper_cfg.signing_delay),
                "submit_rtt_ms": asdict(paper_cfg.submit_rtt),
                "ack_delay_ms": asdict(paper_cfg.ack_delay),
                "confirmed_min_ms": paper_cfg.matched_to_confirmed_min_ms,
                "confirmed_max_ms": paper_cfg.matched_to_confirmed_max_ms,
                "slow_submit_probability": paper_cfg.slow_submit_probability,
                "slow_submit_extra_min_ms": paper_cfg.slow_submit_extra_min_ms,
                "slow_submit_extra_max_ms": paper_cfg.slow_submit_extra_max_ms,
            },
            "orders": orders,
            "fills": fills,
            "rejects": rejects,
            "settlements": settlements,
            "notes": (
                [
                    "Execution feed is populated from the active paper bot state file.",
                    "Submit, ack, and confirm timing are sampled by the realistic paper executor.",
                ]
                if active_execution
                else [
                    (
                        "Order and fill history will populate after the runtime "
                        "publishes order lifecycle events."
                    ),
                    (
                        "Latency distributions are already modeled in paper mode "
                        "but not yet persisted for the dashboard."
                    ),
                ]
            ),
        }

    def system(self) -> dict[str, Any]:
        runtime = self._runtime
        active_system = self._active("system")
        return {
            "runtime": {
                "mode": self._display_runtime_mode(),
                "environment": runtime.config.environment,
                "data_stack": self._display_data_stack(),
                "execution_stack": self._display_execution_stack(),
                "planned_data_stack": list(runtime.summary()["planned_data_stack"]),
                "planned_execution_stack": list(runtime.summary()["planned_execution_stack"]),
                "markets": list(runtime.config.markets),
            },
            "paper_loop": {
                "status": self._display_status(),
                "state_path": active_system.get("state_path", runtime.config.runtime_state_path),
                "feed_mode": active_system.get("feed_mode", "none"),
                "latency_mode": active_system.get(
                    "latency_mode",
                    "submit-time book evaluation with modeled delays",
                ),
                "loop": self._active("loop"),
                "metrics": self._active("metrics"),
            },
            "components": self._component_status(),
            "commands": [
                {
                    "label": "start-live",
                    "description": "Boot the live runtime when Nautilus venue adapters are wired.",
                },
                {
                    "label": "start-paper",
                    "description": (
                        "Boot the realistic paper runtime with the modeled execution path."
                    ),
                },
                {
                    "label": "start-dashboard",
                    "description": "Serve this operator UI and its read-only JSON endpoints.",
                },
            ],
        }

    def settings(self) -> dict[str, Any]:
        runtime = self._runtime
        strategy_settings = asdict(runtime.config.strategy)
        strategy_settings.update(runtime.strategy.profile_summary())
        return {
            "strategy": strategy_settings,
            "freshness": asdict(runtime.config.freshness),
            "kelly": asdict(runtime.config.kelly),
            "paper_execution": asdict(runtime.config.paper_execution),
            "dashboard": asdict(runtime.config.dashboard),
        }

    def bootstrap(self) -> dict[str, Any]:
        return {
            "generated_at": self._display_generated_at(),
            "status": self._display_status(),
            "overview": self.overview(),
            "opportunities": self.opportunities(),
            "portfolio": self.portfolio(),
            "execution": self.execution(),
            "system": self.system(),
            "settings": self.settings(),
            "navigation": [
                {"id": "overview", "label": "Overview"},
                {"id": "execution", "label": "Execution"},
                {"id": "portfolio", "label": "Portfolio"},
                {"id": "system", "label": "Runtime"},
            ],
        }
