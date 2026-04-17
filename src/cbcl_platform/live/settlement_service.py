from __future__ import annotations

from datetime import UTC, datetime

from cbcl_platform.models import LiveMarketBinding, MarketResolution, OutcomeSide
from cbcl_platform.paper import PaperPortfolio


class SettlementService:
    def __init__(self, *, portfolio: PaperPortfolio) -> None:
        self._portfolio = portfolio
        self._resolved_markets: set[str] = set()

    def settle(
        self,
        binding: LiveMarketBinding,
        resolution: MarketResolution,
    ) -> dict[str, object] | None:
        if binding.market_id in self._resolved_markets:
            return None
        winning_side = (
            OutcomeSide.YES
            if resolution.winning_token_id == binding.yes_token_id
            else OutcomeSide.NO
        )
        pnl = self._portfolio.settle_market(binding.market_id, winning_side)
        self._resolved_markets.add(binding.market_id)
        return {
            "time": self._fmt_ts(resolution.resolved_ts_ns),
            "market_id": binding.market_id,
            "coin": binding.coin,
            "interval": binding.interval.value,
            "winning_side": winning_side.value,
            "pnl_usd": round(pnl, 8),
            "source": resolution.source,
        }

    @staticmethod
    def _fmt_ts(ts_ns: int) -> str:
        return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=UTC).strftime("%H:%M:%S")
