from cbcl_platform.config import DelayPercentiles, PaperExecutionConfig
from cbcl_platform.execution import ExecutionCore
from cbcl_platform.models import (
    EntryIntent,
    ExecutionStatus,
    MarketDirection,
    OrderBookLevel,
    OrderBookSnapshot,
    OutcomeSide,
)
from cbcl_platform.paper import InMemoryBookTimeline, RealisticPaperExecutionAdapter


def _ns(ms: int) -> int:
    return ms * 1_000_000


def test_realistic_paper_uses_submit_time_book_snapshot() -> None:
    timeline = InMemoryBookTimeline()
    timeline.add_snapshot(
        "yes",
        _ns(0),
        OrderBookSnapshot(
            token_id="yes",
            asks=(OrderBookLevel(0.70, 10.0),),
            local_receive_ts_ns=_ns(0),
        ),
    )
    timeline.add_snapshot(
        "yes",
        _ns(1_500),
        OrderBookSnapshot(
            token_id="yes",
            asks=(OrderBookLevel(0.92, 10.0),),
            local_receive_ts_ns=_ns(1_500),
        ),
    )

    executor = RealisticPaperExecutionAdapter(
        execution_core=ExecutionCore(),
        config=PaperExecutionConfig(
            random_seed=1,
            internal_delay=DelayPercentiles(p50_ms=10, p95_ms=10, p99_ms=10),
            signing_delay=DelayPercentiles(p50_ms=1_600, p95_ms=1_600, p99_ms=1_600),
            submit_rtt=DelayPercentiles(p50_ms=10, p95_ms=10, p99_ms=10),
            ack_delay=DelayPercentiles(p50_ms=10, p95_ms=10, p99_ms=10),
            slow_submit_probability=0.0,
        ),
    )
    intent = EntryIntent(
        strategy_name="cb_cl_005",
        market_id="mkt1",
        token_id="yes",
        side=OutcomeSide.YES,
        direction=MarketDirection.UP,
        decision_ts_ns=_ns(0),
        signal_price=0.70,
        hard_cap=0.90,
        drift_cap=0.02,
        size_usd=4.0,
        target_shares=5.0,
        expected_profit_usd=1.0,
        confidence=0.95,
    )
    lifecycle = executor.execute_intent(intent, book_timeline=timeline)
    assert lifecycle.status == ExecutionStatus.REJECTED
    assert "hard cap" in lifecycle.reason


def test_realistic_paper_fills_and_updates_balance() -> None:
    timeline = InMemoryBookTimeline()
    timeline.add_snapshot(
        "yes",
        _ns(0),
        OrderBookSnapshot(
            token_id="yes",
            asks=(OrderBookLevel(0.70, 10.0),),
            local_receive_ts_ns=_ns(0),
        ),
    )
    executor = RealisticPaperExecutionAdapter(
        execution_core=ExecutionCore(),
        config=PaperExecutionConfig(random_seed=2),
    )
    intent = EntryIntent(
        strategy_name="cb_cl_005",
        market_id="mkt2",
        token_id="yes",
        side=OutcomeSide.YES,
        direction=MarketDirection.UP,
        decision_ts_ns=_ns(0),
        signal_price=0.70,
        hard_cap=0.90,
        drift_cap=0.02,
        size_usd=4.0,
        target_shares=5.0,
        expected_profit_usd=1.0,
        confidence=0.95,
    )
    lifecycle = executor.execute_intent(intent, book_timeline=timeline)
    assert lifecycle.status in {ExecutionStatus.FILLED, ExecutionStatus.PARTIAL}
    assert executor.portfolio.cash_balance_usd < executor.portfolio.starting_balance_usd
