from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import suppress
from time import perf_counter

from cbcl_platform.models import RuntimeMode
from cbcl_platform.runtime import build_runtime


def _require_live_credentials() -> None:
    required = [
        "POLYMARKET_PK",
        "POLYMARKET_FUNDER",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_PASSPHRASE",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required live Polymarket credentials: {', '.join(missing)}")


async def run_trading_node(
    *,
    mode: RuntimeMode,
    duration_seconds: float = 0.0,
    with_dashboard: bool = False,
) -> int:
    from cbcl_platform.json_http import UrlopenAsyncClient
    from cbcl_platform.live.market_registry import MarketRegistry
    from cbcl_platform.nautilus.polymarket_ids import binding_instrument_ids
    from cbcl_platform.nautilus.recorder import RuntimeRecorder
    from cbcl_platform.nautilus.services import (
        RuntimeServices,
        register_runtime_services,
        unregister_runtime_services,
    )
    from cbcl_platform.state_store import RuntimeStateStore

    bootstrap_started = perf_counter()
    runtime = build_runtime(RuntimeMode.PAPER if mode == RuntimeMode.PAPER else RuntimeMode.LIVE)
    if mode == RuntimeMode.LIVE:
        _require_live_credentials()

    runtime_id = f"{mode.value}-{uuid.uuid4().hex[:8]}"
    state_store = RuntimeStateStore(runtime.config.runtime_state_path)
    http_client = UrlopenAsyncClient(timeout=30.0)
    registry = MarketRegistry(
        http_client=http_client,
        config=runtime.config.market_registry,
        allowed_coins=runtime.config.markets,
    )
    services = RuntimeServices(
        runtime_id=runtime_id,
        runtime=runtime,
        state_store=state_store,
        registry=registry,
        paper_execution=runtime.paper_execution,
        recorder=RuntimeRecorder(runtime.config.recorder),
        bindings={},
        persist_state=not with_dashboard,
    )
    register_runtime_services(services)
    services.set_status("starting")
    services.record_startup_metric("phase", "before_dashboard_start")
    services.write_state(force=True)
    dashboard_handle = None
    if with_dashboard:
        from cbcl_platform import dashboard as serve_dashboard

        dashboard_handle = serve_dashboard.start_dashboard_server(
            runtime=runtime,
            host=runtime.config.dashboard.host,
            port=runtime.config.dashboard.port,
            runtime_id=runtime_id,
        )
    services.record_startup_metric("phase", "before_registry_bootstrap")
    discovery_started = perf_counter()
    initial_bindings = await registry.bootstrap()
    discovery_ms = round((perf_counter() - discovery_started) * 1000.0, 3)
    services.set_bindings(initial_bindings)
    initial_instrument_ids = binding_instrument_ids(list(initial_bindings.values()))
    services.record_startup_metric("registry_bootstrap_ms", discovery_ms)
    services.record_startup_metric("initial_binding_count", len(initial_bindings))
    services.record_startup_metric("initial_instrument_count", len(initial_instrument_ids))
    services.record_startup_metric("phase", "before_nautilus_imports")
    services.write_state(force=True)

    imports_started = perf_counter()
    shims_enabled = (
        mode == RuntimeMode.PAPER
        and str(os.getenv("CBCL_NAUTILUS_COMPAT_SHIMS", "")).lower() in {"1", "true", "yes"}
    )
    services.record_startup_metric("compat_shims_enabled", shims_enabled)
    if shims_enabled:
        from cbcl_platform.nautilus.import_compat import (
            install_lightweight_nautilus_package_shims,
        )
        from cbcl_platform.nautilus.pandas_compat import install_lightweight_pandas_shim

        services.record_startup_metric(
            "pandas_shim_enabled",
            install_lightweight_pandas_shim(),
        )
        services.record_startup_metric(
            "nautilus_package_shims_enabled",
            install_lightweight_nautilus_package_shims(),
        )
    from nautilus_trader.common import Environment
    from nautilus_trader.live.config import RoutingConfig, TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.trading.config import ImportableStrategyConfig

    from cbcl_platform.nautilus.clients import (
        CoinbaseSpotDataClientConfig,
        CoinbaseSpotDataClientFactory,
        PolymarketPaperMarketDataClientConfig,
        PolymarketPaperMarketDataClientFactory,
        PolymarketPaperExecClientConfig,
        PolymarketPaperExecClientFactory,
        PublicPolymarketDataClientFactory,
        RtdsChainlinkDataClientConfig,
        RtdsChainlinkDataClientFactory,
    )
    services.record_startup_metric(
        "nautilus_imports_ms",
        round((perf_counter() - imports_started) * 1000.0, 3),
    )
    services.record_startup_metric("phase", "after_nautilus_imports")
    services.write_state(force=True)

    environment = Environment.SANDBOX if mode == RuntimeMode.PAPER else Environment.LIVE
    provider_cfg = None
    exec_clients = {
        "paper": PolymarketPaperExecClientConfig(
            runtime_id=runtime_id,
            routing=RoutingConfig(venues=frozenset({Venue("POLYMARKET")})),
        )
    }
    polymarket_data_config = PolymarketPaperMarketDataClientConfig(
        runtime_id=runtime_id,
        base_url=runtime.config.polymarket_market_ws.base_url,
        ws_connection_initial_delay_secs=runtime.config.polymarket_market_ws.ws_connection_initial_delay_secs,
        ws_connection_delay_secs=runtime.config.polymarket_market_ws.ws_connection_delay_secs,
        ws_max_subscriptions_per_connection=runtime.config.polymarket_market_ws.ws_max_subscriptions_per_connection,
    )
    polymarket_data_factory = PolymarketPaperMarketDataClientFactory
    live_exec_factory = None
    if mode == RuntimeMode.LIVE:
        from nautilus_trader.adapters.polymarket.config import (
            PolymarketDataClientConfig,
            PolymarketExecClientConfig,
        )
        from nautilus_trader.adapters.polymarket.factories import (
            PolymarketLiveExecClientFactory,
        )
        from nautilus_trader.adapters.polymarket.providers import (
            PolymarketInstrumentProviderConfig,
        )

        provider_cfg = PolymarketInstrumentProviderConfig(
            load_ids=initial_instrument_ids,
            use_gamma_markets=True,
        )

        exec_clients = {
            "polymarket": PolymarketExecClientConfig(
                instrument_config=provider_cfg,
                routing=RoutingConfig(venues=frozenset({Venue("POLYMARKET")})),
            )
        }
        polymarket_data_config = PolymarketDataClientConfig(
            instrument_config=provider_cfg,
            base_url_http="https://clob.polymarket.com",
            base_url_ws=runtime.config.polymarket_market_ws.base_url,
            ws_connection_initial_delay_secs=runtime.config.polymarket_market_ws.ws_connection_initial_delay_secs,
            ws_connection_delay_secs=runtime.config.polymarket_market_ws.ws_connection_delay_secs,
            ws_max_subscriptions_per_connection=runtime.config.polymarket_market_ws.ws_max_subscriptions_per_connection,
            update_instruments_interval_mins=1,
            routing=RoutingConfig(venues=frozenset({Venue("POLYMARKET")})),
        )
        polymarket_data_factory = PublicPolymarketDataClientFactory
        live_exec_factory = PolymarketLiveExecClientFactory
    config = TradingNodeConfig(
        environment=environment,
        data_clients={
            "polymarket": polymarket_data_config,
            "coinbase": CoinbaseSpotDataClientConfig(
                runtime_id=runtime_id,
                base_url=runtime.config.coinbase_ws.base_url,
                reconnect_delay_secs=runtime.config.coinbase_ws.reconnect_delay_secs,
                ping_interval_secs=runtime.config.coinbase_ws.ping_interval_secs,
            ),
            "rtds": RtdsChainlinkDataClientConfig(
                runtime_id=runtime_id,
                base_url=runtime.config.rtds_ws.base_url,
                reconnect_delay_secs=runtime.config.rtds_ws.reconnect_delay_secs,
                ping_interval_secs=runtime.config.rtds_ws.ping_interval_secs,
            ),
        },
        exec_clients=exec_clients,
        strategies=[
            ImportableStrategyConfig(
                strategy_path="cbcl_platform.nautilus.strategy:CBCL005NautilusStrategy",
                config_path="cbcl_platform.nautilus.strategy:CBCL005NautilusStrategyConfig",
                config={
                    "runtime_id": runtime_id,
                    "market_refresh_interval_s": (
                        runtime.config.market_registry.active_refresh_interval_s
                    ),
                    "state_flush_interval_ms": (
                        runtime.config.paper_execution.state_flush_interval_ms
                    ),
                    "order_id_tag": "CBCL005",
                    "oms_type": "NETTING",
                },
            )
        ],
        save_state=False,
        load_state=False,
    )
    node = TradingNode(config=config)
    services.record_startup_metric("phase", "after_node_init")
    services.write_state(force=True)
    node.add_data_client_factory("polymarket", polymarket_data_factory)
    node.add_data_client_factory("coinbase", CoinbaseSpotDataClientFactory)
    node.add_data_client_factory("rtds", RtdsChainlinkDataClientFactory)
    node.add_exec_client_factory("paper", PolymarketPaperExecClientFactory)
    if live_exec_factory is not None:
        node.add_exec_client_factory("polymarket", live_exec_factory)
    build_started = perf_counter()
    services.record_startup_metric("phase", "before_node_build")
    services.write_state(force=True)
    node.build()
    services.record_startup_metric(
        "node_build_ms",
        round((perf_counter() - build_started) * 1000.0, 3),
    )
    services.record_startup_metric(
        "bootstrap_total_ms",
        round((perf_counter() - bootstrap_started) * 1000.0, 3),
    )

    run_task = asyncio.create_task(node.run_async(), name=f"{runtime_id}-node")
    try:
        if duration_seconds > 0:
            await asyncio.sleep(duration_seconds)
            if dashboard_handle is not None:
                dashboard_handle.stop()
                dashboard_handle = None
            await node.stop_async()
        if duration_seconds > 0:
            try:
                await asyncio.wait_for(run_task, timeout=5.0)
            except TimeoutError:
                run_task.cancel()
                with suppress(asyncio.CancelledError):
                    await run_task
        else:
            await run_task
    finally:
        services.set_status("stopped")
        services.write_state(force=True)
        unregister_runtime_services(runtime_id)
        if dashboard_handle is not None:
            dashboard_handle.stop()
        services.close()
        await http_client.aclose()
    return 0
