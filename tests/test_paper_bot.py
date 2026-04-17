import time
from dataclasses import replace

from cbcl_platform.config import DelayPercentiles
from cbcl_platform.models import (
    EntryIntent,
    ExecutionStatus,
    MarketDirection,
    OrderBookLevel,
    OrderBookSnapshot,
    OutcomeSide,
    RuntimeMode,
    StrategyDecision,
)
from cbcl_platform.paper_bot import PaperTradingBot
from cbcl_platform.runtime import build_runtime
from cbcl_platform.state_store import RuntimeStateStore


def test_paper_bot_publishes_live_runtime_state(tmp_path) -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    runtime.config = replace(
        runtime.config,
        runtime_state_path=str(tmp_path / "runtime_state.json"),
        paper_execution=replace(
            runtime.config.paper_execution,
            loop_interval_ms=25,
            state_flush_interval_ms=25,
            market_duration_5m_s=15,
            market_duration_15m_s=20,
        ),
    )
    runtime.paper_execution = runtime.paper_execution.__class__(
        execution_core=runtime.execution_core,
        config=runtime.config.paper_execution,
    )
    store = RuntimeStateStore(runtime.config.runtime_state_path)

    assert PaperTradingBot(runtime=runtime, state_store=store).run(duration_seconds=0.4) == 0

    payload = store.read()
    assert payload is not None
    assert payload["opportunities"]["status"] == "live"
    assert len(payload["opportunities"]["rows"]) > 0
    assert payload["metrics"]["signals_seen"] > 0


def test_paper_bot_rollover_keeps_market_slot_count_stable(tmp_path) -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    runtime.config = replace(
        runtime.config,
        runtime_state_path=str(tmp_path / "runtime_state.json"),
    )
    runtime.paper_execution = runtime.paper_execution.__class__(
        execution_core=runtime.execution_core,
        config=runtime.config.paper_execution,
    )
    store = RuntimeStateStore(runtime.config.runtime_state_path)
    bot = PaperTradingBot(runtime=runtime, state_store=store)
    now_ns = time.time_ns()

    bot._bootstrap_markets(now_ns)
    assert len(bot._markets) == 4

    slot_market_id, market = next(iter(bot._markets.items()))
    replacement = bot._settle_and_roll_market(slot_market_id, market, now_ns + 1)

    assert len(bot._markets) == 4
    assert replacement.descriptor.market_id in bot._markets
    assert slot_market_id not in bot._markets


def test_paper_bot_primes_future_book_before_execution(tmp_path) -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    runtime.config = replace(
        runtime.config,
        runtime_state_path=str(tmp_path / "runtime_state.json"),
        paper_execution=replace(
            runtime.config.paper_execution,
            internal_delay=DelayPercentiles(p50_ms=10, p95_ms=10, p99_ms=10),
            signing_delay=DelayPercentiles(p50_ms=1_600, p95_ms=1_600, p99_ms=1_600),
            submit_rtt=DelayPercentiles(p50_ms=10, p95_ms=10, p99_ms=10),
            ack_delay=DelayPercentiles(p50_ms=10, p95_ms=10, p99_ms=10),
            slow_submit_probability=0.0,
        ),
    )
    runtime.paper_execution = runtime.paper_execution.__class__(
        execution_core=runtime.execution_core,
        config=runtime.config.paper_execution,
    )
    bot = PaperTradingBot(
        runtime=runtime,
        state_store=RuntimeStateStore(runtime.config.runtime_state_path),
    )
    now_ns = time.time_ns()
    bot._bootstrap_markets(now_ns)
    market = next(iter(bot._markets.values()))
    future_cutoff_ns = now_ns + 1_000_000_000

    def _book_for_mid(token_id: str, _mid: float, ts_ns: int) -> OrderBookSnapshot:
        ask_price = 0.70 if ts_ns < future_cutoff_ns else 0.92
        return OrderBookSnapshot(
            token_id=token_id,
            asks=(OrderBookLevel(ask_price, 10.0),),
            bids=(OrderBookLevel(max(0.01, ask_price - 0.02), 10.0),),
            source_event_ts_ns=ts_ns,
            local_receive_ts_ns=ts_ns,
        )

    bot._book_for_mid = _book_for_mid  # type: ignore[method-assign]
    bot._simulate_market_state(market, now_ns)

    decision = StrategyDecision(
        accepted=True,
        reason="ok",
        intent=EntryIntent(
            strategy_name="cb_cl_005",
            market_id=market.descriptor.market_id,
            token_id=market.descriptor.yes_token_id,
            side=OutcomeSide.YES,
            direction=MarketDirection.UP,
            decision_ts_ns=now_ns,
            signal_price=0.70,
            hard_cap=0.90,
            drift_cap=0.02,
            size_usd=4.0,
            target_shares=5.0,
            expected_profit_usd=0.0,
            confidence=0.95,
        ),
    )

    bot._maybe_execute(market, decision, now_ns)

    assert bot._recent_orders
    assert bot._recent_orders[0]["status"] == ExecutionStatus.REJECTED.value
    assert "hard cap" in bot._recent_orders[0]["reason"]


def test_paper_bot_signal_metrics_only_count_real_signals(tmp_path) -> None:
    runtime = build_runtime(RuntimeMode.PAPER)
    runtime.config = replace(
        runtime.config,
        runtime_state_path=str(tmp_path / "runtime_state.json"),
        paper_execution=replace(
            runtime.config.paper_execution,
            loop_interval_ms=25,
            state_flush_interval_ms=25,
            market_duration_5m_s=15,
            market_duration_15m_s=20,
        ),
    )
    runtime.paper_execution = runtime.paper_execution.__class__(
        execution_core=runtime.execution_core,
        config=runtime.config.paper_execution,
    )
    store = RuntimeStateStore(runtime.config.runtime_state_path)

    assert PaperTradingBot(runtime=runtime, state_store=store).run(duration_seconds=0.4) == 0

    payload = store.read()
    assert payload is not None
    metrics = payload["metrics"]
    assert metrics["signals_seen"] >= metrics["signals_accepted"]
    assert metrics["orders_submitted"] == metrics["signals_accepted"]
