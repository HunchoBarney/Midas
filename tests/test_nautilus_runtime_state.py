from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_instrument_id
from nautilus_trader.model.data import CustomData, DataType

from cbcl_platform.models import (
    ContractInterval,
    KellyCalibration,
    LiveMarketBinding,
    OrderBookLevel,
    OrderBookSnapshot,
    PortfolioSnapshot,
    PriceUpdate,
    RuntimeMode,
    StrategyDecision,
    StrategyMarketState,
)
from cbcl_platform.nautilus.data import CoinbaseSpotPrice
from cbcl_platform.nautilus.services import (
    _RECORDED_FEED_EVENT_MIN_INTERVAL_NS,
    RuntimeServices,
)
from cbcl_platform.nautilus.strategy import CBCL005NautilusStrategy
from cbcl_platform.runtime import build_runtime
from cbcl_platform.state_store import RuntimeStateStore


def _binding(
    market_id: str,
    *,
    coin: str = "BTC",
    interval: ContractInterval = ContractInterval.FIVE_MINUTES,
    expires_at_ns: int = 10_000_000_000,
) -> LiveMarketBinding:
    return LiveMarketBinding(
        market_id=market_id,
        event_slug=f"{coin.lower()}-updown",
        coin=coin,
        interval=interval,
        expires_at_ns=expires_at_ns,
        yes_token_id=f"{market_id}-yes",
        no_token_id=f"{market_id}-no",
        condition_id=f"{market_id}-condition",
    )


def _update(symbol: str, price: float, ts_ns: int) -> PriceUpdate:
    return PriceUpdate(
        source="test",
        symbol=symbol,
        price=price,
        source_event_ts_ns=ts_ns,
        local_receive_ts_ns=ts_ns,
    )


def _book(token_id: str, price: float, ts_ns: int) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        asks=(OrderBookLevel(price=price, size=25.0),),
        source_event_ts_ns=ts_ns,
        local_receive_ts_ns=ts_ns,
    )


def _make_services(tmp_path) -> RuntimeServices:
    runtime = build_runtime(RuntimeMode.PAPER)
    store = RuntimeStateStore(str(tmp_path / "runtime_state.json"))
    return RuntimeServices(
        runtime_id="runtime-1",
        runtime=runtime,
        state_store=store,
        registry=None,  # type: ignore[arg-type]
    )


def _make_strategy(runtime, services: RuntimeServices) -> CBCL005NautilusStrategy:
    strategy = CBCL005NautilusStrategy.__new__(CBCL005NautilusStrategy)
    strategy._runtime = runtime  # noqa: SLF001
    strategy._services = services  # noqa: SLF001
    return strategy  # type: ignore[return-value]


def test_runtime_services_prune_old_opportunity_rows_on_binding_roll(tmp_path) -> None:
    services = _make_services(tmp_path)
    old_binding = _binding("old-market", coin="BTC")
    new_binding = _binding("new-market", coin="BTC", interval=ContractInterval.FIFTEEN_MINUTES)
    services.record_opportunity_row(old_binding.market_id, {"market": "BTC 5m"})
    services.record_opportunity_row(new_binding.market_id, {"market": "BTC 15m"})

    services.set_bindings({new_binding.market_id: new_binding})

    assert list(services.opportunity_rows) == [new_binding.market_id]


def test_row_for_clears_skew_stale_reason_once_skew_recovers(tmp_path) -> None:
    services = _make_services(tmp_path)
    runtime = services.runtime
    runtime.config = replace(
        runtime.config,
        freshness=replace(runtime.config.freshness, max_feed_skew_ms=750),
    )
    strategy = _make_strategy(runtime, services)
    binding = _binding("btc-market", coin="BTC")
    now_ns = 5_000_000_000
    state = StrategyMarketState(
        market=binding.to_market_descriptor(),
        coinbase_price=_update("BTC", 101_000.0, now_ns),
        chainlink_price=_update("BTC", 100_000.0, now_ns - 100_000_000),
        yes_book=_book(binding.yes_token_id, 0.61, now_ns),
        no_book=_book(binding.no_token_id, 0.59, now_ns),
    )
    services.stale_reasons["BTC"] = "feed skew gate (5100ms)"

    strategy._row_for(  # noqa: SLF001
        binding,
        StrategyDecision(False, "threshold not met"),
        state,
        100.0,
        now_ns,
    )

    assert "BTC" not in services.stale_reasons


def test_evaluate_market_counts_only_accepted_signals(tmp_path) -> None:
    services = _make_services(tmp_path)
    runtime = services.runtime
    strategy = _make_strategy(runtime, services)
    binding = _binding("btc-market", coin="BTC")
    now_ns = 5_000_000_000
    strategy._current_bindings = {binding.market_id: binding}  # noqa: SLF001
    strategy._latest_coinbase = {"BTC": _update("BTC", 101_000.0, now_ns)}  # noqa: SLF001
    strategy._latest_chainlink = {"BTC": _update("BTC", 100_000.0, now_ns)}  # noqa: SLF001
    strategy._book_snapshot = lambda *_args, **_kwargs: None  # noqa: SLF001
    strategy._portfolio_snapshot = (  # noqa: SLF001
        lambda _binding: PortfolioSnapshot(
            cash_balance_usd=150.0,
            total_exposure_usd=0.0,
            open_positions=0,
        )
    )
    strategy._calibration = lambda: KellyCalibration()  # noqa: SLF001

    strategy._evaluate_market(binding.market_id, now_ns)  # noqa: SLF001

    assert services.signals_seen == 0
    assert services.opportunity_rows[binding.market_id]["reason"] == "missing polymarket book"


def test_runtime_services_snapshot_exposes_startup_metrics_and_feed_state(tmp_path) -> None:
    services = _make_services(tmp_path)
    now_ns = __import__("time").time_ns()
    services.record_startup_metric("registry_bootstrap_ms", 12.5)
    services.mark_feed_connected("coinbase")
    services.mark_feed_connected("polymarket_market")
    services.record_polymarket_book_event(now_ns)

    payload = services.snapshot_payload()

    assert payload["system"]["startup"]["metrics"]["registry_bootstrap_ms"] == 12.5
    assert payload["system"]["startup"]["first_data_ts_ns"]["polymarket_book"] == now_ns
    assert payload["system"]["feeds"]["coinbase"]["state"] == "warmup"
    assert payload["system"]["feeds"]["polymarket_market"]["state"] == "healthy"


def test_runtime_services_skip_periodic_disk_write_when_persist_disabled(tmp_path) -> None:
    services = _make_services(tmp_path)
    services.persist_state = False

    services.write_state()

    assert not services.state_store.path.exists()

    services.write_state(force=True)

    assert services.state_store.path.exists()


def test_runtime_services_throttle_redundant_feed_event_recording(tmp_path) -> None:
    services = _make_services(tmp_path)
    events: list[tuple[str, dict[str, object]]] = []
    services._record = lambda event_type, payload: events.append((event_type, payload))  # type: ignore[method-assign]

    services.mark_feed_event("polymarket_market", 1_000_000_000)
    services.mark_feed_event(
        "polymarket_market",
        1_000_000_000 + _RECORDED_FEED_EVENT_MIN_INTERVAL_NS - 1,
    )
    services.mark_feed_event(
        "polymarket_market",
        1_000_000_000 + _RECORDED_FEED_EVENT_MIN_INTERVAL_NS,
    )

    assert [event_type for event_type, _payload in events] == ["feed_event", "feed_event"]


def test_ensure_instrument_subscription_requests_then_subscribes_once_loaded(
    tmp_path,
    monkeypatch,
) -> None:
    services = _make_services(tmp_path)
    runtime = services.runtime
    strategy = _make_strategy(runtime, services)
    binding = _binding("btc-market", coin="BTC")
    instrument_id = str(
        get_polymarket_instrument_id(
            binding.condition_id,
            binding.yes_token_id,
        )
    )
    cached: dict[str, object] = {}
    requested: list[str] = []
    subscribed: list[str] = []

    fake_cache = SimpleNamespace(
        instrument=lambda instrument: cached.get(str(instrument)),
    )
    monkeypatch.setattr(
        CBCL005NautilusStrategy,
        "cache",
        property(lambda _self: fake_cache),
        raising=False,
    )
    strategy.request_instrument = lambda instrument: requested.append(str(instrument))  # type: ignore[attr-defined]
    strategy.subscribe_order_book_deltas = (  # type: ignore[attr-defined]
        lambda instrument: subscribed.append(str(instrument))
    )
    strategy._subscribed_instruments = set()  # noqa: SLF001
    strategy._pending_instrument_requests = {}  # noqa: SLF001

    strategy._ensure_instrument_subscription(  # noqa: SLF001
        instrument_id,
        now_ns=1_000_000_000,
        request_retry_ns=500_000_000,
    )

    assert requested == [instrument_id]
    assert subscribed == []
    assert strategy._pending_instrument_requests[instrument_id] == 1_000_000_000  # noqa: SLF001

    cached[instrument_id] = object()
    strategy._ensure_instrument_subscription(  # noqa: SLF001
        instrument_id,
        now_ns=2_000_000_000,
        request_retry_ns=500_000_000,
    )

    assert subscribed == [instrument_id]
    assert instrument_id in strategy._subscribed_instruments  # noqa: SLF001
    assert instrument_id not in strategy._pending_instrument_requests  # noqa: SLF001


def test_strategy_on_data_unwraps_custom_data_payload(tmp_path) -> None:
    services = _make_services(tmp_path)
    runtime = services.runtime
    strategy = _make_strategy(runtime, services)
    marked: list[str] = []
    strategy._latest_coinbase = {}  # noqa: SLF001
    strategy._mark_coin_dirty = lambda coin: marked.append(coin)  # type: ignore[method-assign]

    payload = CoinbaseSpotPrice(
        coin="BTC",
        symbol="BTC",
        price=75000.0,
        source_event_ts_ns=10,
        local_receive_ts_ns=11,
        volume_24h=100.0,
        ts_event=10,
        ts_init=11,
    )

    strategy.on_data(CustomData(DataType(CoinbaseSpotPrice), payload))

    assert strategy._latest_coinbase["BTC"].price == 75000.0  # noqa: SLF001
    assert marked == ["BTC"]
