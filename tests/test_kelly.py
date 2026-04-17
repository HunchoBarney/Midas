from cbcl_platform.config import KellyConfig
from cbcl_platform.kelly import KellySizingEngine
from cbcl_platform.models import KellyCalibration, PortfolioSnapshot


def test_kelly_uses_bootstrap_fixed_size_below_enable_threshold() -> None:
    engine = KellySizingEngine(config=KellyConfig())
    result = engine.size_order(
        buy_price=0.80,
        confidence=0.95,
        portfolio=PortfolioSnapshot(cash_balance_usd=100.0, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
    )
    assert result.accepted is True
    assert result.bootstrap_mode == "BOOTSTRAP_FIXED"
    assert result.target_shares == 5.0
    assert result.target_size_usd == 4.0


def test_kelly_activates_above_balance_threshold_and_sizes_fractionally() -> None:
    engine = KellySizingEngine(config=KellyConfig())
    result = engine.size_order(
        buy_price=0.70,
        confidence=0.95,
        portfolio=PortfolioSnapshot(cash_balance_usd=1_000.0, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
    )
    assert result.accepted is True
    assert result.bootstrap_mode == "KELLY_ACTIVE"
    assert result.target_size_usd > 5.0
    assert result.bet_fraction > 0.0


def test_kelly_rejects_when_there_is_no_edge() -> None:
    engine = KellySizingEngine(config=KellyConfig())
    result = engine.size_order(
        buy_price=0.90,
        confidence=0.55,
        portfolio=PortfolioSnapshot(cash_balance_usd=1_000.0, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.70),
    )
    assert result.accepted is False
    assert "no edge" in result.reason


def test_kelly_uses_remaining_cash_without_subtracting_exposure_twice() -> None:
    engine = KellySizingEngine(config=KellyConfig())
    result = engine.size_order(
        buy_price=0.70,
        confidence=0.95,
        portfolio=PortfolioSnapshot(cash_balance_usd=900.0, total_exposure_usd=100.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
    )
    assert result.accepted is True
    assert result.target_size_usd == 45.0


def test_kelly_rejects_when_minimum_ticket_exceeds_bankroll() -> None:
    engine = KellySizingEngine(config=KellyConfig())
    result = engine.size_order(
        buy_price=0.80,
        confidence=0.95,
        portfolio=PortfolioSnapshot(cash_balance_usd=3.90, total_exposure_usd=0.0),
        calibration=KellyCalibration(trade_count=100, win_rate=0.99),
    )
    assert result.accepted is False
    assert "minimum ticket exceeds available bankroll" in result.reason
