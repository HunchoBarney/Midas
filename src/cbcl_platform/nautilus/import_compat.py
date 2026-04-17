from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import sys
import types
from decimal import ROUND_HALF_UP
from decimal import Decimal
from pathlib import Path


def _nautilus_root() -> Path | None:
    spec = importlib.util.find_spec("nautilus_trader")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def _install_lazy_package(
    name: str,
    *,
    package_path: Path,
    export_map: dict[str, str],
    extra_exports: dict[str, object] | None = None,
) -> bool:
    if name in sys.modules:
        return False

    module = types.ModuleType(name)
    module.__file__ = str(package_path / "__init__.py")
    module.__package__ = name
    module.__path__ = [str(package_path)]
    module.__spec__ = importlib.machinery.ModuleSpec(
        name=name,
        loader=None,
        is_package=True,
    )
    module.__all__ = sorted({*export_map, *(extra_exports or {})})

    def __getattr__(attr: str):
        if extra_exports and attr in extra_exports:
            value = extra_exports[attr]
            setattr(module, attr, value)
            return value
        target = export_map.get(attr)
        if target is None:
            raise AttributeError(f"module {name!r} has no attribute {attr!r}")
        value = getattr(importlib.import_module(target), attr)
        setattr(module, attr, value)
        return value

    module.__getattr__ = __getattr__  # type: ignore[attr-defined]
    sys.modules[name] = module
    return True


def _model_extra_exports() -> dict[str, object]:
    from nautilus_trader.core import nautilus_pyo3

    def convert_to_raw_int(value, precision: int) -> int:
        fixed_precision = getattr(
            importlib.import_module("nautilus_trader.model.objects"),
            "FIXED_PRECISION",
        )
        decimal_value = Decimal(str(value))
        quantized = decimal_value.quantize(Decimal(10) ** -precision, rounding=ROUND_HALF_UP)
        return int(quantized * (10**fixed_precision))

    class _LazyTuple:
        def __iter__(self):
            return iter(tuple(self))

        def __len__(self):
            return len(tuple(self))

        def __getitem__(self, item):
            return tuple(self)[item]

    class _BookDataTypes(_LazyTuple):
        def __iter__(self):
            model_data = importlib.import_module("nautilus_trader.model.data")
            return iter(
                {
                    model_data.OrderBookDelta,
                    model_data.OrderBookDeltas,
                    model_data.OrderBookDepth10,
                },
            )

    class _Pyo3DataTypes(_LazyTuple):
        def __iter__(self):
            return iter(
                (
                    nautilus_pyo3.OrderBookDelta,
                    nautilus_pyo3.OrderBookDepth10,
                    nautilus_pyo3.QuoteTick,
                    nautilus_pyo3.TradeTick,
                    nautilus_pyo3.Bar,
                ),
            )

    return {
        "convert_to_raw_int": convert_to_raw_int,
        "BOOK_DATA_TYPES": _BookDataTypes(),
        "NAUTILUS_PYO3_DATA_TYPES": _Pyo3DataTypes(),
    }


def install_lightweight_nautilus_package_shims() -> bool:
    root = _nautilus_root()
    if root is None:
        return False

    config_exports = {
        "BacktestDataConfig": "nautilus_trader.backtest.config",
        "BacktestEngineConfig": "nautilus_trader.backtest.config",
        "BacktestRunConfig": "nautilus_trader.backtest.config",
        "BacktestVenueConfig": "nautilus_trader.backtest.config",
        "FeeModelFactory": "nautilus_trader.backtest.config",
        "FillModelConfig": "nautilus_trader.backtest.config",
        "FillModelFactory": "nautilus_trader.backtest.config",
        "FixedFeeModelConfig": "nautilus_trader.backtest.config",
        "FXRolloverInterestConfig": "nautilus_trader.backtest.config",
        "ImportableFeeModelConfig": "nautilus_trader.backtest.config",
        "ImportableFillModelConfig": "nautilus_trader.backtest.config",
        "ImportableLatencyModelConfig": "nautilus_trader.backtest.config",
        "LatencyModelConfig": "nautilus_trader.backtest.config",
        "LatencyModelFactory": "nautilus_trader.backtest.config",
        "MakerTakerFeeModelConfig": "nautilus_trader.backtest.config",
        "PerContractFeeModelConfig": "nautilus_trader.backtest.config",
        "SimulationModuleConfig": "nautilus_trader.backtest.config",
        "CacheConfig": "nautilus_trader.cache.config",
        "ActorConfig": "nautilus_trader.common.config",
        "ActorFactory": "nautilus_trader.common.config",
        "DatabaseConfig": "nautilus_trader.common.config",
        "ImportableActorConfig": "nautilus_trader.common.config",
        "ImportableConfig": "nautilus_trader.common.config",
        "InstrumentProviderConfig": "nautilus_trader.common.config",
        "InvalidConfiguration": "nautilus_trader.common.config",
        "LoggingConfig": "nautilus_trader.common.config",
        "MessageBusConfig": "nautilus_trader.common.config",
        "NautilusConfig": "nautilus_trader.common.config",
        "NonNegativeFloat": "nautilus_trader.common.config",
        "NonNegativeInt": "nautilus_trader.common.config",
        "OrderEmulatorConfig": "nautilus_trader.common.config",
        "PositiveFloat": "nautilus_trader.common.config",
        "PositiveInt": "nautilus_trader.common.config",
        "msgspec_decoding_hook": "nautilus_trader.common.config",
        "msgspec_encoding_hook": "nautilus_trader.common.config",
        "register_config_decoding": "nautilus_trader.common.config",
        "register_config_encoding": "nautilus_trader.common.config",
        "resolve_config_path": "nautilus_trader.common.config",
        "resolve_path": "nautilus_trader.common.config",
        "tokenize_config": "nautilus_trader.common.config",
        "DataEngineConfig": "nautilus_trader.data.config",
        "ExecAlgorithmConfig": "nautilus_trader.execution.config",
        "ExecAlgorithmFactory": "nautilus_trader.execution.config",
        "ExecEngineConfig": "nautilus_trader.execution.config",
        "ImportableExecAlgorithmConfig": "nautilus_trader.execution.config",
        "ControllerConfig": "nautilus_trader.live.config",
        "ControllerFactory": "nautilus_trader.live.config",
        "ImportableControllerConfig": "nautilus_trader.live.config",
        "LiveDataClientConfig": "nautilus_trader.live.config",
        "LiveDataEngineConfig": "nautilus_trader.live.config",
        "LiveExecClientConfig": "nautilus_trader.live.config",
        "LiveExecEngineConfig": "nautilus_trader.live.config",
        "LiveRiskEngineConfig": "nautilus_trader.live.config",
        "RoutingConfig": "nautilus_trader.live.config",
        "TradingNodeConfig": "nautilus_trader.live.config",
        "DataCatalogConfig": "nautilus_trader.persistence.config",
        "StreamingConfig": "nautilus_trader.persistence.config",
        "PortfolioConfig": "nautilus_trader.portfolio.config",
        "RiskEngineConfig": "nautilus_trader.risk.config",
        "NautilusKernelConfig": "nautilus_trader.system.config",
        "ImportableStrategyConfig": "nautilus_trader.trading.config",
        "StrategyConfig": "nautilus_trader.trading.config",
        "StrategyFactory": "nautilus_trader.trading.config",
    }
    model_exports = {
        "AccountBalance": "nautilus_trader.model.objects",
        "AccountId": "nautilus_trader.model.identifiers",
        "Bar": "nautilus_trader.model.data",
        "BarSpecification": "nautilus_trader.model.data",
        "BarType": "nautilus_trader.model.data",
        "BookLevel": "nautilus_trader.model.book",
        "BookOrder": "nautilus_trader.model.data",
        "ClientId": "nautilus_trader.model.identifiers",
        "ClientOrderId": "nautilus_trader.model.identifiers",
        "ComponentId": "nautilus_trader.model.identifiers",
        "Currency": "nautilus_trader.model.objects",
        "CustomData": "nautilus_trader.model.data",
        "DataType": "nautilus_trader.model.data",
        "ExecAlgorithmId": "nautilus_trader.model.identifiers",
        "FundingRateUpdate": "nautilus_trader.model.data",
        "InstrumentClose": "nautilus_trader.model.data",
        "InstrumentId": "nautilus_trader.model.identifiers",
        "InstrumentStatus": "nautilus_trader.model.data",
        "MarginBalance": "nautilus_trader.model.objects",
        "MarkPriceUpdate": "nautilus_trader.model.data",
        "Money": "nautilus_trader.model.objects",
        "OrderBook": "nautilus_trader.model.book",
        "OrderBookDelta": "nautilus_trader.model.data",
        "OrderBookDeltas": "nautilus_trader.model.data",
        "OrderBookDepth10": "nautilus_trader.model.data",
        "OrderListId": "nautilus_trader.model.identifiers",
        "Position": "nautilus_trader.model.position",
        "PositionId": "nautilus_trader.model.identifiers",
        "Price": "nautilus_trader.model.objects",
        "Quantity": "nautilus_trader.model.objects",
        "QuoteTick": "nautilus_trader.model.data",
        "StrategyId": "nautilus_trader.model.identifiers",
        "Symbol": "nautilus_trader.model.identifiers",
        "TradeId": "nautilus_trader.model.identifiers",
        "TradeTick": "nautilus_trader.model.data",
        "TraderId": "nautilus_trader.model.identifiers",
        "Venue": "nautilus_trader.model.identifiers",
        "VenueOrderId": "nautilus_trader.model.identifiers",
    }

    changed = False
    changed |= _install_lazy_package(
        "nautilus_trader.config",
        package_path=root / "config",
        export_map=config_exports,
    )
    changed |= _install_lazy_package(
        "nautilus_trader.model",
        package_path=root / "model",
        export_map=model_exports,
        extra_exports=_model_extra_exports(),
    )
    return changed
