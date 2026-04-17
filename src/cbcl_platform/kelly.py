from __future__ import annotations

from dataclasses import dataclass

from cbcl_platform.config import KellyConfig
from cbcl_platform.models import KellyCalibration, KellySizingResult, PortfolioSnapshot


@dataclass
class KellySizingEngine:
    config: KellyConfig
    _bootstrap_kelly_active: bool = False

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, float(value)))

    def _bootstrap_mode(self, available_balance_usd: float) -> str:
        if not self.config.bootstrap_fixed_shares_enabled:
            return "OFF"

        enable_at = max(0.0, float(self.config.bootstrap_enable_balance_usd))
        disable_at = min(enable_at, max(0.0, float(self.config.bootstrap_disable_balance_usd)))

        if self._bootstrap_kelly_active:
            if available_balance_usd + 1e-9 < disable_at:
                self._bootstrap_kelly_active = False
        elif available_balance_usd + 1e-9 >= enable_at:
            self._bootstrap_kelly_active = True

        return "KELLY_ACTIVE" if self._bootstrap_kelly_active else "BOOTSTRAP_FIXED"

    def size_order(
        self,
        *,
        buy_price: float,
        confidence: float,
        portfolio: PortfolioSnapshot,
        calibration: KellyCalibration,
    ) -> KellySizingResult:
        if buy_price <= 0.0 or buy_price >= 1.0:
            return KellySizingResult(
                accepted=False,
                reason="invalid buy price",
                target_size_usd=0.0,
                target_shares=0.0,
                bootstrap_mode=self._bootstrap_mode(portfolio.available_bankroll_usd),
                win_prob=0.0,
                full_kelly=0.0,
                bet_fraction=0.0,
            )

        bankroll = float(portfolio.available_bankroll_usd)
        if bankroll <= 0.0:
            return KellySizingResult(
                accepted=False,
                reason="no available bankroll",
                target_size_usd=0.0,
                target_shares=0.0,
                bootstrap_mode=self._bootstrap_mode(bankroll),
                win_prob=0.0,
                full_kelly=0.0,
                bet_fraction=0.0,
            )

        bootstrap_mode = self._bootstrap_mode(bankroll)
        min_notional = max(0.01, self.config.min_shares * buy_price)
        if not self.config.enabled:
            target_size_usd = min_notional
            if target_size_usd > bankroll + 1e-9:
                return KellySizingResult(
                    accepted=False,
                    reason="minimum ticket exceeds available bankroll",
                    target_size_usd=0.0,
                    target_shares=0.0,
                    bootstrap_mode=bootstrap_mode,
                    win_prob=0.0,
                    full_kelly=0.0,
                    bet_fraction=0.0,
                    metadata={
                        "bankroll": bankroll,
                        "bootstrap_mode": bootstrap_mode,
                        "trade_count": int(calibration.trade_count),
                    },
                )
            return KellySizingResult(
                accepted=True,
                reason="disabled",
                target_size_usd=target_size_usd,
                target_shares=round(target_size_usd / buy_price, 8),
                bootstrap_mode=bootstrap_mode,
                win_prob=0.0,
                full_kelly=0.0,
                bet_fraction=0.0,
                metadata={
                    "bankroll": bankroll,
                    "bootstrap_mode": bootstrap_mode,
                    "trade_count": int(calibration.trade_count),
                },
            )

        empirical_wr = self._clamp(float(calibration.win_rate or 0.0), 0.01, 0.99)
        confidence_prob = self._clamp(
            0.5 + ((self._clamp(confidence, 0.01, 0.99) - 0.5) * self.config.confidence_blend),
            0.01,
            0.99,
        )
        history_weight = min(
            1.0,
            float(max(0, calibration.trade_count))
            / float(max(1, self.config.min_trades_for_full_trust)),
        )
        win_prob = self._clamp(
            (history_weight * empirical_wr) + ((1.0 - history_weight) * confidence_prob),
            0.01,
            0.99,
        )

        full_kelly = (win_prob - buy_price) / max(1e-9, 1.0 - buy_price)
        if full_kelly <= 0.0:
            return KellySizingResult(
                accepted=False,
                reason=f"no edge (p={win_prob:.3f} <= q={buy_price:.3f})",
                target_size_usd=0.0,
                target_shares=0.0,
                bootstrap_mode=bootstrap_mode,
                win_prob=win_prob,
                full_kelly=full_kelly,
                bet_fraction=0.0,
                metadata={"bankroll": bankroll, "trade_count": int(calibration.trade_count)},
            )

        bet_fraction = min(
            max(0.0, self.config.max_bankroll_fraction),
            max(0.0, self.config.fraction) * full_kelly,
        )
        if bet_fraction <= 0.0:
            return KellySizingResult(
                accepted=False,
                reason="kelly fraction is zero",
                target_size_usd=0.0,
                target_shares=0.0,
                bootstrap_mode=bootstrap_mode,
                win_prob=win_prob,
                full_kelly=full_kelly,
                bet_fraction=bet_fraction,
                metadata={"bankroll": bankroll, "trade_count": int(calibration.trade_count)},
            )

        target_size_usd = bankroll * bet_fraction
        if bootstrap_mode == "BOOTSTRAP_FIXED":
            target_size_usd = min_notional
        else:
            target_size_usd = max(min_notional, target_size_usd)

        if target_size_usd > bankroll + 1e-9:
            return KellySizingResult(
                accepted=False,
                reason="minimum ticket exceeds available bankroll",
                target_size_usd=0.0,
                target_shares=0.0,
                bootstrap_mode=bootstrap_mode,
                win_prob=win_prob,
                full_kelly=full_kelly,
                bet_fraction=bet_fraction,
                metadata={"bankroll": bankroll, "trade_count": int(calibration.trade_count)},
            )

        if target_size_usd < 0.01:
            return KellySizingResult(
                accepted=False,
                reason="kelly target below minimum notional",
                target_size_usd=0.0,
                target_shares=0.0,
                bootstrap_mode=bootstrap_mode,
                win_prob=win_prob,
                full_kelly=full_kelly,
                bet_fraction=bet_fraction,
                metadata={"bankroll": bankroll, "trade_count": int(calibration.trade_count)},
            )

        return KellySizingResult(
            accepted=True,
            reason="ok",
            target_size_usd=round(target_size_usd, 8),
            target_shares=round(target_size_usd / buy_price, 8),
            bootstrap_mode=bootstrap_mode,
            win_prob=win_prob,
            full_kelly=full_kelly,
            bet_fraction=bet_fraction,
            metadata={
                "bankroll": bankroll,
                "trade_count": int(calibration.trade_count),
                "empirical_wr": empirical_wr,
                "confidence_prob": confidence_prob,
                "bootstrap_mode": bootstrap_mode,
            },
        )
