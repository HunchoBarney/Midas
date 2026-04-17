from cbcl_platform.config import FreshnessConfig, KellyConfig, StrategyConfig
from cbcl_platform.execution import ExecutionCore
from cbcl_platform.kelly import KellySizingEngine
from cbcl_platform.models import (
    ContractInterval,
    EntryIntent,
    KellyCalibration,
    MarketDescriptor,
    MarketDirection,
    OrderBookLevel,
    OrderBookSnapshot,
    OutcomeSide,
    PortfolioSnapshot,
    PriceUpdate,
    StrategyMarketState,
)
from cbcl_platform.strategy import CbClDivergenceStrategy


def _ns(ms: int) -> int:
    return ms * 1_000_000


def test_strategy_emits_cbcl_intent_when_threshold_hits() -> None:
    strategy = CbClDivergenceStrategy(
        config=StrategyConfig(),
        freshness=FreshnessConfig(),
        kelly=KellySizingEngine(config=KellyConfig()),
    )
    now_ns = _ns(10_000)
    market = MarketDescriptor(
        market_id="mkt1",
        event_slug="btc-updown",
        coin="BTC",
        interval=ContractInterval.FIVE_MINUTES,
        expires_at_ns=now_ns + _ns(90_000),
        yes_token_id="yes",
        no_token_id="no",
    )
    decision = strategy.evaluate(
        state=StrategyMarketState(
            market=market,
            coinbase_price=PriceUpdate(
                "coinbase", "BTC", 100.10, now_ns - _ns(50), now_ns - _ns(50)
            ),
            chainlink_price=PriceUpdate(
                "chainlink", "BTC", 100.00, now_ns - _ns(50), now_ns - _ns(50)
            ),
            yes_book=OrderBookSnapshot(
                token_id="yes",
                asks=(OrderBookLevel(0.70, 10.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
            no_book=OrderBookSnapshot(
                token_id="no",
                asks=(OrderBookLevel(0.30, 10.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
        ),
        portfolio=PortfolioSnapshot(cash_balance_usd=1_000.0, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
        now_ns=now_ns,
    )
    assert decision.accepted is True
    assert decision.intent is not None
    assert decision.intent.side == "YES"
    assert decision.intent.hard_cap == 0.90


def test_cbcl005_allows_stale_non_selected_book_like_loguetown() -> None:
    strategy = CbClDivergenceStrategy(
        config=StrategyConfig(strategy_name="cb_cl_005"),
        freshness=FreshnessConfig(),
        kelly=KellySizingEngine(config=KellyConfig()),
    )
    now_ns = _ns(10_000)
    market = MarketDescriptor(
        market_id="mkt1",
        event_slug="btc-updown",
        coin="BTC",
        interval=ContractInterval.FIVE_MINUTES,
        expires_at_ns=now_ns + _ns(90_000),
        yes_token_id="yes",
        no_token_id="no",
    )
    decision = strategy.evaluate(
        state=StrategyMarketState(
            market=market,
            coinbase_price=PriceUpdate(
                "coinbase", "BTC", 100.10, now_ns - _ns(50), now_ns - _ns(50)
            ),
            chainlink_price=PriceUpdate(
                "chainlink", "BTC", 100.00, now_ns - _ns(50), now_ns - _ns(50)
            ),
            yes_book=OrderBookSnapshot(
                token_id="yes",
                asks=(OrderBookLevel(0.70, 10.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
            no_book=OrderBookSnapshot(
                token_id="no",
                asks=(OrderBookLevel(0.30, 10.0),),
                local_receive_ts_ns=now_ns - _ns(5_000),
            ),
        ),
        portfolio=PortfolioSnapshot(cash_balance_usd=1_000.0, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
        now_ns=now_ns,
    )
    assert decision.accepted is True
    assert decision.intent is not None
    assert decision.intent.side == OutcomeSide.YES


def test_cbcl005_signal_can_pass_above_hard_cap_for_execution_recheck() -> None:
    strategy = CbClDivergenceStrategy(
        config=StrategyConfig(strategy_name="cb_cl_005"),
        freshness=FreshnessConfig(),
        kelly=KellySizingEngine(config=KellyConfig()),
    )
    now_ns = _ns(10_000)
    market = MarketDescriptor(
        market_id="mkt1",
        event_slug="btc-updown",
        coin="BTC",
        interval=ContractInterval.FIVE_MINUTES,
        expires_at_ns=now_ns + _ns(90_000),
        yes_token_id="yes",
        no_token_id="no",
    )
    decision = strategy.evaluate(
        state=StrategyMarketState(
            market=market,
            coinbase_price=PriceUpdate(
                "coinbase", "BTC", 100.10, now_ns - _ns(50), now_ns - _ns(50)
            ),
            chainlink_price=PriceUpdate(
                "chainlink", "BTC", 100.00, now_ns - _ns(50), now_ns - _ns(50)
            ),
            yes_book=OrderBookSnapshot(
                token_id="yes",
                asks=(OrderBookLevel(0.91, 20.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
            no_book=OrderBookSnapshot(
                token_id="no",
                asks=(OrderBookLevel(0.09, 20.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
        ),
        portfolio=PortfolioSnapshot(cash_balance_usd=1_000.0, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
        now_ns=now_ns,
    )
    assert decision.accepted is True
    assert decision.intent is not None
    assert decision.intent.signal_price == 0.91
    assert decision.intent.hard_cap == 0.90


def test_cbcl005_does_not_block_low_selected_ask_in_live_mode() -> None:
    strategy = CbClDivergenceStrategy(
        config=StrategyConfig(strategy_name="cb_cl_005"),
        freshness=FreshnessConfig(),
        kelly=KellySizingEngine(config=KellyConfig()),
    )
    now_ns = _ns(10_000)
    market = MarketDescriptor(
        market_id="mkt1",
        event_slug="btc-updown",
        coin="BTC",
        interval=ContractInterval.FIVE_MINUTES,
        expires_at_ns=now_ns + _ns(90_000),
        yes_token_id="yes",
        no_token_id="no",
    )
    decision = strategy.evaluate(
        state=StrategyMarketState(
            market=market,
            coinbase_price=PriceUpdate(
                "coinbase", "BTC", 100.10, now_ns - _ns(50), now_ns - _ns(50)
            ),
            chainlink_price=PriceUpdate(
                "chainlink", "BTC", 100.00, now_ns - _ns(50), now_ns - _ns(50)
            ),
            yes_book=OrderBookSnapshot(
                token_id="yes",
                asks=(OrderBookLevel(0.24, 20.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
            no_book=OrderBookSnapshot(
                token_id="no",
                asks=(OrderBookLevel(0.76, 20.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
        ),
        portfolio=PortfolioSnapshot(cash_balance_usd=1_000.0, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
        now_ns=now_ns,
    )
    assert decision.accepted is True
    assert decision.intent is not None
    assert decision.intent.signal_price == 0.24


def test_cbcl005_uses_entry_window_grace_like_loguetown() -> None:
    strategy = CbClDivergenceStrategy(
        config=StrategyConfig(strategy_name="cb_cl_005"),
        freshness=FreshnessConfig(),
        kelly=KellySizingEngine(config=KellyConfig()),
    )
    now_ns = _ns(10_000)
    market = MarketDescriptor(
        market_id="mkt1",
        event_slug="btc-updown",
        coin="BTC",
        interval=ContractInterval.FIVE_MINUTES,
        expires_at_ns=now_ns + _ns(132_000),
        yes_token_id="yes",
        no_token_id="no",
    )
    decision = strategy.evaluate(
        state=StrategyMarketState(
            market=market,
            coinbase_price=PriceUpdate(
                "coinbase", "BTC", 100.10, now_ns - _ns(50), now_ns - _ns(50)
            ),
            chainlink_price=PriceUpdate(
                "chainlink", "BTC", 100.00, now_ns - _ns(50), now_ns - _ns(50)
            ),
            yes_book=OrderBookSnapshot(
                token_id="yes",
                asks=(OrderBookLevel(0.70, 10.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
            no_book=OrderBookSnapshot(
                token_id="no",
                asks=(OrderBookLevel(0.30, 10.0),),
                local_receive_ts_ns=now_ns - _ns(40),
            ),
        ),
        portfolio=PortfolioSnapshot(cash_balance_usd=1_000.0, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
        now_ns=now_ns,
    )
    assert decision.accepted is True


def test_execution_core_rejects_when_requote_exceeds_hard_cap() -> None:
    core = ExecutionCore()
    book = OrderBookSnapshot(
        token_id="yes",
        asks=(
            OrderBookLevel(0.89, 1.0),
            OrderBookLevel(0.90, 1.0),
            OrderBookLevel(0.94, 10.0),
        ),
        local_receive_ts_ns=_ns(100),
    )
    intent = EntryIntent(
        strategy_name="cb_cl_005",
        market_id="mkt1",
        token_id="yes",
        side=OutcomeSide.YES,
        direction=MarketDirection.UP,
        decision_ts_ns=_ns(100),
        signal_price=0.89,
        hard_cap=0.90,
        drift_cap=0.02,
        size_usd=10.0,
        target_shares=5.0,
        expected_profit_usd=0.0,
        confidence=0.95,
    )
    decision = core.decide(intent=intent, book=book)
    assert decision.action.value == "reject"
    assert "hard cap" in decision.reason
