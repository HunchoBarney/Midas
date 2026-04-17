from __future__ import annotations

from dataclasses import dataclass

from cbcl_platform.constants import clamp_binary_price
from cbcl_platform.models import BookQuote, ExecutionAction, ExecutionDecision, OrderBookSnapshot


@dataclass
class ExecutionCore:
    allow_partial_fills: bool = True

    @staticmethod
    def _limit_for_target_size(
        asks: tuple,
        target_shares: float,
    ) -> tuple[float, float]:
        cumulative = 0.0
        price = asks[-1].price
        for level in asks:
            cumulative += level.size
            price = level.price
            if cumulative + 1e-9 >= target_shares:
                break
        return price, cumulative

    def quote_buy(
        self,
        book: OrderBookSnapshot,
        *,
        signal_price: float,
        target_shares: float,
    ) -> BookQuote:
        capped_signal = clamp_binary_price(signal_price)
        clean_target = round(max(0.01, target_shares), 8)
        if not book.asks:
            return BookQuote(
                signal_price=capped_signal,
                executable_price=0.0,
                target_shares=clean_target,
                executable_shares=0.0,
                target_reachable=False,
                telemetry={"mode": "no_asks"},
            )

        best_ask = book.asks[0].price
        best_size = book.asks[0].size
        target_ask, cumulative = self._limit_for_target_size(book.asks, clean_target)
        levels_crossed = 0
        for level in book.asks:
            levels_crossed += 1
            if level.price + 1e-9 >= target_ask:
                break

        next_ask = book.asks[1].price if len(book.asks) > 1 else target_ask
        spread_ticks = max(0.0, next_ask - best_ask)
        depth_ratio = cumulative / max(clean_target, 1e-9)
        target_jump = max(0.0, target_ask - best_ask)
        if spread_ticks >= 0.02 or target_jump >= 0.02:
            cushion = 0.02
        elif levels_crossed >= 3 or depth_ratio < 1.25 or best_size < max(1.0, 0.25 * clean_target):
            cushion = 0.01
        else:
            cushion = 0.00

        executable_price = clamp_binary_price(target_ask + cushion)
        executable_shares = book.shares_available_at_or_below(executable_price)
        return BookQuote(
            signal_price=capped_signal,
            executable_price=executable_price,
            target_shares=clean_target,
            executable_shares=executable_shares,
            target_reachable=executable_shares + 1e-9 >= clean_target,
            telemetry={
                "mode": "adaptive_book",
                "best_ask": round(best_ask, 4),
                "best_size": round(best_size, 4),
                "target_ask": round(target_ask, 4),
                "cumulative_at_target": round(cumulative, 4),
                "levels_crossed": levels_crossed,
                "spread_ticks": round(spread_ticks, 4),
                "target_jump": round(target_jump, 4),
                "cushion": round(cushion, 4),
                "depth_ratio": round(depth_ratio, 4),
            },
        )

    def decide(self, *, intent, book: OrderBookSnapshot) -> ExecutionDecision:
        quote = self.quote_buy(
            book,
            signal_price=float(intent.signal_price),
            target_shares=float(intent.target_shares),
        )
        if quote.executable_price <= 0.0:
            return ExecutionDecision(
                action=ExecutionAction.REJECT,
                reason="no executable asks",
                limit_price=0.0,
                expected_shares=0.0,
                quote=quote,
            )

        hard_cap = float(intent.hard_cap)
        max_live_price = float(intent.signal_price) + float(intent.drift_cap)
        if quote.executable_price > hard_cap + 1e-9:
            return ExecutionDecision(
                action=ExecutionAction.REJECT,
                reason=f"hard cap exceeded ({quote.executable_price:.2f} > {hard_cap:.2f})",
                limit_price=0.0,
                expected_shares=0.0,
                quote=quote,
            )
        if quote.executable_price > max_live_price + 1e-9:
            return ExecutionDecision(
                action=ExecutionAction.REJECT,
                reason=(
                    f"reprice drift exceeded ({quote.executable_price:.2f} > "
                    f"{float(intent.signal_price):.2f}+{float(intent.drift_cap):.2f})"
                ),
                limit_price=0.0,
                expected_shares=0.0,
                quote=quote,
            )

        expected_shares = min(float(intent.target_shares), quote.executable_shares)
        if expected_shares <= 0.0:
            return ExecutionDecision(
                action=ExecutionAction.REJECT,
                reason="no shares available inside cap",
                limit_price=0.0,
                expected_shares=0.0,
                quote=quote,
            )

        if not self.allow_partial_fills and expected_shares + 1e-9 < float(intent.target_shares):
            return ExecutionDecision(
                action=ExecutionAction.REJECT,
                reason="partial fill not allowed",
                limit_price=0.0,
                expected_shares=0.0,
                quote=quote,
            )

        return ExecutionDecision(
            action=ExecutionAction.SUBMIT,
            reason="ok",
            limit_price=quote.executable_price,
            expected_shares=round(expected_shares, 8),
            quote=quote,
            metadata={"target_reachable": quote.target_reachable},
        )
