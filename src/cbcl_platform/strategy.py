from __future__ import annotations

import time
from dataclasses import dataclass

from cbcl_platform.config import FreshnessConfig, StrategyConfig
from cbcl_platform.constants import GAS_COST_PER_ORDER_USD, fee_rate_for_price
from cbcl_platform.kelly import KellySizingEngine
from cbcl_platform.models import (
    ContractInterval,
    EntryIntent,
    KellyCalibration,
    MarketDirection,
    OutcomeSide,
    PortfolioSnapshot,
    StrategyDecision,
    StrategyMarketState,
)


@dataclass(frozen=True)
class SignalProfile:
    max_minutes_to_close: float
    entry_window_grace_minutes: float
    min_buy_price: float
    max_signal_buy_price: float | None
    require_fresh_selected_book: bool


class CbClDivergenceStrategy:
    LEGACY_SIGNAL_PROFILES: dict[str, dict[str, object]] = {
        "cb_cl_005": {
            "max_minutes_to_close": 2.0,
            "min_buy_price": 0.0,
            "max_signal_buy_price": None,
            "require_fresh_selected_book": True,
        },
    }

    def __init__(
        self,
        *,
        config: StrategyConfig,
        freshness: FreshnessConfig,
        kelly: KellySizingEngine,
    ) -> None:
        self.config = config
        self.freshness = freshness
        self.kelly = kelly

    def _max_minutes_to_close(self, interval: ContractInterval) -> float:
        if interval == ContractInterval.FIVE_MINUTES:
            return float(self.config.max_minutes_to_close_5m)
        if interval == ContractInterval.HOURLY:
            return float(self.config.max_minutes_to_close_15m)
        return float(self.config.max_minutes_to_close_15m)

    def _signal_profile(self, interval: ContractInterval) -> SignalProfile:
        strategy_name = str(self.config.strategy_name or "").strip().lower()
        overrides = self.LEGACY_SIGNAL_PROFILES.get(strategy_name, {})
        return SignalProfile(
            max_minutes_to_close=float(
                overrides.get("max_minutes_to_close", self._max_minutes_to_close(interval))
            ),
            entry_window_grace_minutes=max(
                0.0,
                float(self.config.entry_window_grace_minutes),
            ),
            min_buy_price=float(
                overrides.get("min_buy_price", self.config.min_buy_price)
            ),
            max_signal_buy_price=(
                None
                if overrides.get("max_signal_buy_price", self.config.max_buy_price) is None
                else float(overrides.get("max_signal_buy_price", self.config.max_buy_price))
            ),
            require_fresh_selected_book=bool(
                overrides.get("require_fresh_selected_book", True)
            ),
        )

    def profile_summary(self) -> dict[str, object]:
        five_min = self._signal_profile(ContractInterval.FIVE_MINUTES)
        fifteen_min = self._signal_profile(ContractInterval.FIFTEEN_MINUTES)
        return {
            "strategy_name": self.config.strategy_name,
            "threshold": self.config.threshold,
            "signal_confidence": self.config.signal_confidence,
            "min_buy_price": self.config.min_buy_price,
            "hard_cap": self.config.max_buy_price,
            "max_price_drift": self.config.max_price_drift,
            "entry_window_grace_minutes": self.config.entry_window_grace_minutes,
            "signal_min_buy_price": five_min.min_buy_price,
            "signal_max_buy_price": five_min.max_signal_buy_price,
            "signal_require_fresh_selected_book": five_min.require_fresh_selected_book,
            "signal_max_minutes_to_close_5m": five_min.max_minutes_to_close,
            "signal_max_minutes_to_close_15m": fifteen_min.max_minutes_to_close,
            "signal_max_minutes_to_close_1h": self._signal_profile(
                ContractInterval.HOURLY
            ).max_minutes_to_close,
        }

    def evaluate(
        self,
        state: StrategyMarketState,
        *,
        portfolio: PortfolioSnapshot,
        calibration: KellyCalibration,
        now_ns: int | None = None,
    ) -> StrategyDecision:
        now_ns = now_ns or time.time_ns()
        profile = self._signal_profile(state.market.interval)
        if state.coinbase_price is None or state.chainlink_price is None:
            return StrategyDecision(False, "missing external price feed")
        if state.yes_book is None or state.no_book is None:
            return StrategyDecision(False, "missing polymarket book")

        coinbase_age = state.coinbase_price.age_ms(now_ns)
        chainlink_age = state.chainlink_price.age_ms(now_ns)
        if coinbase_age > self.freshness.coinbase_age_ms:
            return StrategyDecision(False, f"coinbase stale ({coinbase_age:.0f}ms)")
        if chainlink_age > self.freshness.chainlink_age_ms:
            return StrategyDecision(False, f"chainlink stale ({chainlink_age:.0f}ms)")

        minutes_left = state.market.minutes_to_close(now_ns)
        max_minutes = profile.max_minutes_to_close
        if minutes_left <= 0.0:
            return StrategyDecision(False, "market expired")
        if minutes_left > max_minutes + profile.entry_window_grace_minutes + 1e-9:
            return StrategyDecision(
                False,
                (
                    f"time gate ({minutes_left:.2f}m > "
                    f"{max_minutes:.2f}m+{profile.entry_window_grace_minutes:.2f}m)"
                ),
            )

        chainlink_price = float(state.chainlink_price.price)
        if chainlink_price <= 0.0:
            return StrategyDecision(False, "invalid chainlink price")
        coinbase_price = float(state.coinbase_price.price)
        divergence = (coinbase_price - chainlink_price) / chainlink_price
        if abs(divergence) < self.config.threshold:
            return StrategyDecision(False, "threshold not met")

        if divergence > 0.0:
            direction = MarketDirection.UP
            side = OutcomeSide.YES
            token_id = state.market.yes_token_id
            book = state.yes_book
        else:
            direction = MarketDirection.DOWN
            side = OutcomeSide.NO
            token_id = state.market.no_token_id
            book = state.no_book

        selected_book_age = book.age_ms(now_ns)
        if (
            profile.require_fresh_selected_book
            and selected_book_age > self.freshness.polymarket_book_age_ms
        ):
            return StrategyDecision(
                False,
                f"selected book stale ({selected_book_age:.0f}ms)",
            )

        best_ask = book.best_ask()
        if best_ask is None:
            return StrategyDecision(False, "no asks in selected book")
        if best_ask < profile.min_buy_price:
            return StrategyDecision(False, f"best ask below min ({best_ask:.2f})")
        if (
            profile.max_signal_buy_price is not None
            and best_ask > profile.max_signal_buy_price
        ):
            return StrategyDecision(
                False,
                f"best ask above signal max ({best_ask:.2f})",
            )

        sizing = self.kelly.size_order(
            buy_price=best_ask,
            confidence=self.config.signal_confidence,
            portfolio=portfolio,
            calibration=calibration,
        )
        if not sizing.accepted:
            return StrategyDecision(
                False,
                f"kelly gate: {sizing.reason}",
                metadata={"kelly": sizing.metadata},
            )

        trade_fee = sizing.target_size_usd * fee_rate_for_price(best_ask)
        expected_profit = (
            sizing.target_shares * 1.0 - sizing.target_size_usd - trade_fee - GAS_COST_PER_ORDER_USD
        )
        intent = EntryIntent(
            strategy_name=self.config.strategy_name,
            market_id=state.market.market_id,
            token_id=token_id,
            side=side,
            direction=direction,
            decision_ts_ns=now_ns,
            signal_price=best_ask,
            hard_cap=self.config.max_buy_price,
            drift_cap=self.config.max_price_drift,
            size_usd=sizing.target_size_usd,
            target_shares=sizing.target_shares,
            expected_profit_usd=round(expected_profit, 8),
            confidence=self.config.signal_confidence,
            metadata={
                "coin": state.market.coin,
                "coinbase_price": coinbase_price,
                "chainlink_price": chainlink_price,
                "divergence": divergence,
                "minutes_to_close": round(minutes_left, 6),
                "selected_book_age_ms": round(selected_book_age, 3),
                "signal_min_buy_price": profile.min_buy_price,
                "signal_max_buy_price": profile.max_signal_buy_price,
                "signal_require_fresh_selected_book": profile.require_fresh_selected_book,
                "signal_entry_window_grace_minutes": profile.entry_window_grace_minutes,
                "kelly_bootstrap_mode": sizing.bootstrap_mode,
                "kelly_win_prob": sizing.win_prob,
                "kelly_full_fraction": sizing.full_kelly,
                "kelly_bet_fraction": sizing.bet_fraction,
                "kelly_target_size_usd": sizing.target_size_usd,
                "trade_fee_usd": round(trade_fee, 8),
            },
        )
        return StrategyDecision(True, "ok", intent=intent, metadata={"kelly": sizing.metadata})
