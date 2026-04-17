from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from math import log
from typing import Protocol

from cbcl_platform.config import DelayPercentiles, PaperExecutionConfig
from cbcl_platform.constants import GAS_COST_PER_ORDER_USD, fee_rate_for_price
from cbcl_platform.execution import ExecutionCore
from cbcl_platform.models import (
    EntryIntent,
    ExecutionAction,
    ExecutionStatus,
    FillResult,
    OrderBookSnapshot,
    OrderLifecycle,
    OutcomeSide,
    PaperPosition,
    PortfolioSnapshot,
)


class BookTimeline(Protocol):
    def snapshot_for(self, token_id: str, ts_ns: int) -> OrderBookSnapshot | None: ...


@dataclass
class InMemoryBookTimeline:
    snapshots: dict[str, list[tuple[int, OrderBookSnapshot]]] = field(default_factory=dict)
    max_snapshots_per_token: int = 512

    def add_snapshot(self, token_id: str, ts_ns: int, snapshot: OrderBookSnapshot) -> None:
        self.snapshots.setdefault(token_id, []).append((ts_ns, snapshot))
        self.snapshots[token_id].sort(key=lambda item: item[0])
        if len(self.snapshots[token_id]) > self.max_snapshots_per_token:
            self.snapshots[token_id] = self.snapshots[token_id][-self.max_snapshots_per_token :]

    def snapshot_for(self, token_id: str, ts_ns: int) -> OrderBookSnapshot | None:
        candidates = self.snapshots.get(token_id, [])
        latest: OrderBookSnapshot | None = None
        for snapshot_ts, snapshot in candidates:
            if snapshot_ts <= ts_ns:
                latest = snapshot
            else:
                break
        return latest


@dataclass
class PaperPortfolio:
    starting_balance_usd: float
    cash_balance_usd: float = field(init=False)
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    realized_pnl_usd: float = 0.0

    def __post_init__(self) -> None:
        self.cash_balance_usd = float(self.starting_balance_usd)

    @property
    def total_exposure_usd(self) -> float:
        return round(sum(position.cost_basis_usd for position in self.positions.values()), 8)

    def snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            cash_balance_usd=round(self.cash_balance_usd, 8),
            total_exposure_usd=round(self.total_exposure_usd, 8),
            open_positions=len(self.positions),
        )

    def can_afford(self, total_required_usd: float) -> bool:
        return self.cash_balance_usd + 1e-9 >= float(total_required_usd)

    def apply_fill(self, intent: EntryIntent, fill: FillResult) -> None:
        self.cash_balance_usd -= fill.total_cost + fill.trade_fee_usd + fill.gas_fee_usd
        position = self.positions.setdefault(
            intent.market_id, PaperPosition(market_id=intent.market_id)
        )
        if intent.side == OutcomeSide.YES:
            position.yes_shares += fill.filled_shares
        else:
            position.no_shares += fill.filled_shares
        position.cost_basis_usd += fill.total_cost
        position.fees_paid_usd += fill.trade_fee_usd + fill.gas_fee_usd

    def settle_market(self, market_id: str, winning_side: OutcomeSide) -> float:
        position = self.positions.pop(market_id, None)
        if position is None:
            return 0.0
        winning_shares = (
            position.yes_shares if winning_side == OutcomeSide.YES else position.no_shares
        )
        payout = winning_shares * 1.0
        self.cash_balance_usd += payout
        pnl = payout - position.cost_basis_usd - position.fees_paid_usd
        self.realized_pnl_usd += pnl
        return round(pnl, 8)


class DelaySampler:
    def __init__(self, *, seed: int) -> None:
        self._rng = random.Random(seed)

    def _sample_lognormal_ms(self, percentiles: DelayPercentiles) -> int:
        mu = log(max(1.0, percentiles.p50_ms))
        sigma = max(
            0.01,
            (log(max(percentiles.p95_ms, percentiles.p50_ms + 1)) - mu) / 1.644854,
        )
        sample = self._rng.lognormvariate(mu, sigma)
        return int(min(max(sample, 0.0), percentiles.p99_ms))

    def sample_internal_ms(self, percentiles: DelayPercentiles) -> int:
        return self._sample_lognormal_ms(percentiles)

    def sample_signing_ms(self, percentiles: DelayPercentiles) -> int:
        return self._sample_lognormal_ms(percentiles)

    def sample_network_ms(self, percentiles: DelayPercentiles) -> int:
        return self._sample_lognormal_ms(percentiles)

    def sample_ack_ms(self, percentiles: DelayPercentiles) -> int:
        return self._sample_lognormal_ms(percentiles)

    def sample_confirm_ms(self, cfg: PaperExecutionConfig) -> int:
        return self._rng.randint(
            cfg.matched_to_confirmed_min_ms,
            cfg.matched_to_confirmed_max_ms,
        )

    def maybe_add_slow_tail_ms(self, cfg: PaperExecutionConfig) -> int:
        if self._rng.random() >= cfg.slow_submit_probability:
            return 0
        return self._rng.randint(cfg.slow_submit_extra_min_ms, cfg.slow_submit_extra_max_ms)


@dataclass
class RealisticPaperExecutionAdapter:
    execution_core: ExecutionCore
    config: PaperExecutionConfig
    portfolio: PaperPortfolio = field(init=False)
    _sampler: DelaySampler = field(init=False)

    def __post_init__(self) -> None:
        self.portfolio = PaperPortfolio(starting_balance_usd=self.config.initial_balance_usd)
        self._sampler = DelaySampler(seed=self.config.random_seed)

    def sample_timing(self, decision_ts_ns: int) -> ExecutionTiming:
        internal_ms = self._sampler.sample_internal_ms(self.config.internal_delay)
        signing_ms = self._sampler.sample_signing_ms(self.config.signing_delay)
        submit_rtt_ms = self._sampler.sample_network_ms(self.config.submit_rtt)
        slow_tail_ms = self._sampler.maybe_add_slow_tail_ms(self.config)
        submit_rtt_ms += slow_tail_ms
        ack_delay_ms = self._sampler.sample_ack_ms(self.config.ack_delay)
        confirm_delay_ms = self._sampler.sample_confirm_ms(self.config)
        submit_ts_ns = decision_ts_ns + (internal_ms + signing_ms + submit_rtt_ms) * 1_000_000
        ack_ts_ns = submit_ts_ns + (ack_delay_ms * 1_000_000)
        return ExecutionTiming(
            internal_ms=internal_ms,
            signing_ms=signing_ms,
            submit_rtt_ms=submit_rtt_ms,
            slow_tail_ms=slow_tail_ms,
            ack_delay_ms=ack_delay_ms,
            confirm_delay_ms=confirm_delay_ms,
            submit_ts_ns=submit_ts_ns,
            ack_ts_ns=ack_ts_ns,
        )

    def execute_intent(
        self,
        intent: EntryIntent,
        *,
        book_timeline: BookTimeline,
        matching_engine_blocked: bool = False,
        timing: ExecutionTiming | None = None,
    ) -> OrderLifecycle:
        timing = timing or self.sample_timing(intent.decision_ts_ns)
        submit_ts_ns = timing.submit_ts_ns
        ack_ts_ns = timing.ack_ts_ns

        if matching_engine_blocked:
            fill = FillResult(
                status=ExecutionStatus.REJECTED,
                filled_shares=0.0,
                average_price=0.0,
                total_cost=0.0,
                trade_fee_usd=0.0,
                gas_fee_usd=0.0,
            )
            return OrderLifecycle(
                order_id=self._order_id(),
                market_id=intent.market_id,
                token_id=intent.token_id,
                side=intent.side,
                status=ExecutionStatus.REJECTED,
                reason="matching engine restart window active",
                decision_ts_ns=intent.decision_ts_ns,
                submit_ts_ns=submit_ts_ns,
                ack_ts_ns=ack_ts_ns,
                fill_ts_ns=None,
                confirmed_ts_ns=None,
                limit_price=0.0,
                requested_shares=float(intent.target_shares),
                fill=fill,
            )

        book = book_timeline.snapshot_for(intent.token_id, submit_ts_ns)
        if book is None:
            fill = FillResult(
                status=ExecutionStatus.REJECTED,
                filled_shares=0.0,
                average_price=0.0,
                total_cost=0.0,
                trade_fee_usd=0.0,
                gas_fee_usd=0.0,
            )
            return OrderLifecycle(
                order_id=self._order_id(),
                market_id=intent.market_id,
                token_id=intent.token_id,
                side=intent.side,
                status=ExecutionStatus.REJECTED,
                reason="no book snapshot at simulated submit time",
                decision_ts_ns=intent.decision_ts_ns,
                submit_ts_ns=submit_ts_ns,
                ack_ts_ns=ack_ts_ns,
                fill_ts_ns=None,
                confirmed_ts_ns=None,
                limit_price=0.0,
                requested_shares=float(intent.target_shares),
                fill=fill,
            )

        decision = self.execution_core.decide(intent=intent, book=book)
        if decision.action == ExecutionAction.REJECT:
            fill = FillResult(
                status=ExecutionStatus.REJECTED,
                filled_shares=0.0,
                average_price=0.0,
                total_cost=0.0,
                trade_fee_usd=0.0,
                gas_fee_usd=0.0,
            )
            return OrderLifecycle(
                order_id=self._order_id(),
                market_id=intent.market_id,
                token_id=intent.token_id,
                side=intent.side,
                status=ExecutionStatus.REJECTED,
                reason=decision.reason,
                decision_ts_ns=intent.decision_ts_ns,
                submit_ts_ns=submit_ts_ns,
                ack_ts_ns=ack_ts_ns,
                fill_ts_ns=None,
                confirmed_ts_ns=None,
                limit_price=decision.limit_price,
                requested_shares=float(intent.target_shares),
                fill=fill,
                metadata={"quote": decision.quote.telemetry},
            )

        simulated_fill = book.buy_fill(decision.limit_price, float(intent.target_shares))
        if simulated_fill.filled_shares <= 0.0:
            fill = FillResult(
                status=ExecutionStatus.REJECTED,
                filled_shares=0.0,
                average_price=0.0,
                total_cost=0.0,
                trade_fee_usd=0.0,
                gas_fee_usd=0.0,
            )
            return OrderLifecycle(
                order_id=self._order_id(),
                market_id=intent.market_id,
                token_id=intent.token_id,
                side=intent.side,
                status=ExecutionStatus.REJECTED,
                reason="ioc unfilled at simulated submit time",
                decision_ts_ns=intent.decision_ts_ns,
                submit_ts_ns=submit_ts_ns,
                ack_ts_ns=ack_ts_ns,
                fill_ts_ns=None,
                confirmed_ts_ns=None,
                limit_price=decision.limit_price,
                requested_shares=float(intent.target_shares),
                fill=fill,
                metadata={"quote": decision.quote.telemetry},
            )

        trade_fee_usd = simulated_fill.total_cost * fee_rate_for_price(simulated_fill.avg_price)
        gas_fee_usd = GAS_COST_PER_ORDER_USD
        total_required = simulated_fill.total_cost + trade_fee_usd + gas_fee_usd
        if not self.portfolio.can_afford(total_required):
            fill = FillResult(
                status=ExecutionStatus.REJECTED,
                filled_shares=0.0,
                average_price=0.0,
                total_cost=0.0,
                trade_fee_usd=0.0,
                gas_fee_usd=0.0,
            )
            return OrderLifecycle(
                order_id=self._order_id(),
                market_id=intent.market_id,
                token_id=intent.token_id,
                side=intent.side,
                status=ExecutionStatus.REJECTED,
                reason="insufficient paper balance",
                decision_ts_ns=intent.decision_ts_ns,
                submit_ts_ns=submit_ts_ns,
                ack_ts_ns=ack_ts_ns,
                fill_ts_ns=None,
                confirmed_ts_ns=None,
                limit_price=decision.limit_price,
                requested_shares=float(intent.target_shares),
                fill=fill,
                metadata={"quote": decision.quote.telemetry},
            )

        status = ExecutionStatus.FILLED
        if simulated_fill.filled_shares + 1e-9 < float(intent.target_shares):
            status = ExecutionStatus.PARTIAL
        fill = FillResult(
            status=status,
            filled_shares=simulated_fill.filled_shares,
            average_price=simulated_fill.avg_price,
            total_cost=simulated_fill.total_cost,
            trade_fee_usd=round(trade_fee_usd, 8),
            gas_fee_usd=round(gas_fee_usd, 8),
        )
        self.portfolio.apply_fill(intent, fill)
        fill_ts_ns = ack_ts_ns
        confirmed_ts_ns = fill_ts_ns + (timing.confirm_delay_ms * 1_000_000)
        return OrderLifecycle(
            order_id=self._order_id(),
            market_id=intent.market_id,
            token_id=intent.token_id,
            side=intent.side,
            status=status,
            reason="ok",
            decision_ts_ns=intent.decision_ts_ns,
            submit_ts_ns=submit_ts_ns,
            ack_ts_ns=ack_ts_ns,
            fill_ts_ns=fill_ts_ns,
            confirmed_ts_ns=confirmed_ts_ns,
            limit_price=decision.limit_price,
            requested_shares=float(intent.target_shares),
            fill=fill,
            metadata={"quote": decision.quote.telemetry, "decision": decision.metadata},
        )

    @staticmethod
    def _order_id() -> str:
        return str(uuid.uuid4())[:8]


@dataclass(frozen=True)
class ExecutionTiming:
    internal_ms: int
    signing_ms: int
    submit_rtt_ms: int
    slow_tail_ms: int
    ack_delay_ms: int
    confirm_delay_ms: int
    submit_ts_ns: int
    ack_ts_ns: int
