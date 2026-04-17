from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any

import msgspec
import websockets
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import (
    LiveDataClientConfig,
    LiveExecClientConfig,
    PositiveFloat,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import RequestData, SubscribeData, UnsubscribeData
from nautilus_trader.execution.messages import (
    CancelAllOrders,
    CancelOrder,
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    ModifyOrder,
    SubmitOrder,
    SubmitOrderList,
)
from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory
from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.data import BookOrder, CustomData, DataType, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import (
    AccountType,
    BookAction,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    RecordFlag,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import AccountBalance, Money

from cbcl_platform.live.coinbase_data_client import CoinbaseDataClient
from cbcl_platform.live.polymarket_books import PolymarketBookService
from cbcl_platform.live.rtds_data_client import RtdsDataClient
from cbcl_platform.models import EntryIntent, ExecutionStatus, OrderLifecycle
from cbcl_platform.nautilus.data import (
    ChainlinkOraclePrice,
    CoinbaseSpotPrice,
    make_polymarket_instrument,
)
from cbcl_platform.nautilus.polymarket_ids import polymarket_instrument_id
from cbcl_platform.nautilus.services import get_runtime_services

POLYMARKET_VENUE = Venue("POLYMARKET")


class CoinbaseSpotDataClientConfig(LiveDataClientConfig, frozen=True):
    runtime_id: str = ""
    base_url: str = "wss://ws-feed.exchange.coinbase.com"
    reconnect_delay_secs: PositiveFloat = 1.0
    ping_interval_secs: PositiveFloat = 15.0


class RtdsChainlinkDataClientConfig(LiveDataClientConfig, frozen=True):
    runtime_id: str = ""
    base_url: str = "wss://ws-live-data.polymarket.com"
    reconnect_delay_secs: PositiveFloat = 1.0
    ping_interval_secs: PositiveFloat = 5.0


class PolymarketPaperExecClientConfig(LiveExecClientConfig, frozen=True):
    runtime_id: str = ""
    venue: str = "POLYMARKET"


class PolymarketPaperMarketDataClientConfig(LiveDataClientConfig, frozen=True):
    runtime_id: str = ""
    base_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"
    ws_connection_initial_delay_secs: PositiveFloat = 0.25
    ws_connection_delay_secs: PositiveFloat = 0.1
    ws_max_subscriptions_per_connection: int = 200


class PolymarketPaperMarketDataClient(LiveMarketDataClient):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: PolymarketPaperMarketDataClientConfig,
        name: str,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name),
            venue=POLYMARKET_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=InstrumentProvider(),
            config=config,
        )
        self._config = config
        self._services = get_runtime_services(config.runtime_id)
        self._book_service = PolymarketBookService(
            loop=loop,
            clock=clock,
            config=replace(
                self._services.runtime.config.polymarket_market_ws,
                base_url=config.base_url,
                ws_connection_initial_delay_secs=config.ws_connection_initial_delay_secs,
                ws_connection_delay_secs=config.ws_connection_delay_secs,
                ws_max_subscriptions_per_connection=config.ws_max_subscriptions_per_connection,
            ),
            emit=lambda event: self._loop.call_soon_threadsafe(self._handle_book_event, event),
            timeline=self._services.timeline,
        )

    async def _connect(self) -> None:
        self._publish_current_instruments()
        await self._sync_books()

    async def _disconnect(self) -> None:
        await self._book_service.stop()

    async def _subscribe(self, command: SubscribeData) -> None:
        return None

    async def _unsubscribe(self, command: UnsubscribeData) -> None:
        return None

    async def _request(self, request: RequestData) -> None:
        return None

    async def _subscribe_instruments(self, command) -> None:  # type: ignore[override]
        self._publish_current_instruments()

    async def _subscribe_instrument(self, command) -> None:  # type: ignore[override]
        self._publish_instrument(command.instrument_id)

    async def _subscribe_order_book_deltas(self, command) -> None:  # type: ignore[override]
        self._publish_instrument(command.instrument_id)
        await self._sync_books()

    async def _subscribe_order_book_depth(self, command) -> None:  # type: ignore[override]
        await self._subscribe_order_book_deltas(command)

    async def _subscribe_quote_ticks(self, command) -> None:  # type: ignore[override]
        self._publish_instrument(command.instrument_id)

    async def _subscribe_trade_ticks(self, command) -> None:  # type: ignore[override]
        self._publish_instrument(command.instrument_id)

    async def _subscribe_mark_prices(self, command) -> None:  # type: ignore[override]
        return None

    async def _subscribe_index_prices(self, command) -> None:  # type: ignore[override]
        return None

    async def _subscribe_funding_rates(self, command) -> None:  # type: ignore[override]
        return None

    async def _subscribe_bars(self, command) -> None:  # type: ignore[override]
        return None

    async def _subscribe_instrument_status(self, command) -> None:  # type: ignore[override]
        return None

    async def _subscribe_instrument_close(self, command) -> None:  # type: ignore[override]
        return None

    async def _unsubscribe_instruments(self, command) -> None:  # type: ignore[override]
        await self._sync_books()

    async def _unsubscribe_instrument(self, command) -> None:  # type: ignore[override]
        await self._sync_books()

    async def _unsubscribe_order_book_deltas(self, command) -> None:  # type: ignore[override]
        await self._sync_books()

    async def _unsubscribe_order_book_depth(self, command) -> None:  # type: ignore[override]
        await self._sync_books()

    async def _unsubscribe_quote_ticks(self, command) -> None:  # type: ignore[override]
        return None

    async def _unsubscribe_trade_ticks(self, command) -> None:  # type: ignore[override]
        return None

    async def _unsubscribe_mark_prices(self, command) -> None:  # type: ignore[override]
        return None

    async def _unsubscribe_index_prices(self, command) -> None:  # type: ignore[override]
        return None

    async def _unsubscribe_funding_rates(self, command) -> None:  # type: ignore[override]
        return None

    async def _unsubscribe_bars(self, command) -> None:  # type: ignore[override]
        return None

    async def _unsubscribe_instrument_status(self, command) -> None:  # type: ignore[override]
        return None

    async def _unsubscribe_instrument_close(self, command) -> None:  # type: ignore[override]
        return None

    async def _request_instrument(self, request) -> None:  # type: ignore[override]
        self._publish_instrument(request.instrument_id)
        await self._sync_books()

    async def _request_instruments(self, request) -> None:  # type: ignore[override]
        self._publish_current_instruments()
        await self._sync_books()

    async def _request_quote_ticks(self, request) -> None:  # type: ignore[override]
        return None

    async def _request_trade_ticks(self, request) -> None:  # type: ignore[override]
        return None

    async def _request_funding_rates(self, request) -> None:  # type: ignore[override]
        return None

    async def _request_bars(self, request) -> None:  # type: ignore[override]
        return None

    async def _request_order_book_deltas(self, request) -> None:  # type: ignore[override]
        return None

    async def _request_order_book_depth(self, request) -> None:  # type: ignore[override]
        return None

    async def _request_order_book_snapshot(self, request) -> None:  # type: ignore[override]
        return None

    def _publish_current_instruments(self) -> None:
        for binding in self._services.bindings.values():
            self._publish_binding_instruments(binding)

    def _publish_binding_instruments(self, binding) -> None:  # type: ignore[no-untyped-def]
        for token_id in (binding.yes_token_id, binding.no_token_id):
            instrument = make_polymarket_instrument(binding, token_id, ts_init=self._clock.timestamp_ns())
            if self._instrument_provider.find(instrument.id) is None:
                self._instrument_provider.add(instrument)
            self._handle_data(instrument)

    def _publish_instrument(self, instrument_id: InstrumentId) -> None:
        binding = self._binding_for_instrument(instrument_id)
        if binding is None:
            return
        self._publish_binding_instruments(binding)

    async def _sync_books(self) -> None:
        token_ids = {
            token_id
            for binding in self._services.bindings.values()
            for token_id in (binding.yes_token_id, binding.no_token_id)
        }
        await self._book_service.sync_tokens(token_ids)

    def _handle_book_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "feed_status":
            connected = bool(event.get("connected"))
            detail = str(event.get("detail") or "")
            if connected:
                self._services.mark_feed_connected("polymarket_market", detail=detail)
            else:
                self._services.mark_feed_disconnected("polymarket_market", detail=detail)
            self._services.write_state()
            return
        if event_type == "feed_event":
            ts_ns = int(event.get("ts_ns") or self._clock.timestamp_ns())
            self._services.mark_feed_event("polymarket_market", ts_ns)
            return
        if event_type != "book_snapshot":
            return

        snapshot = event.get("snapshot")
        if snapshot is None:
            return
        instrument = self._instrument_for_token(snapshot.token_id)
        if instrument is None:
            return
        deltas = self._snapshot_to_deltas(instrument, snapshot)
        if deltas is not None:
            self._handle_data(deltas)

    def _instrument_for_token(self, token_id: str):
        binding = self._binding_for_token(token_id)
        if binding is None:
            return None
        instrument_id = polymarket_instrument_id(binding.condition_id, token_id)
        instrument = self._cache.instrument(instrument_id)
        if instrument is not None:
            return instrument
        instrument = make_polymarket_instrument(binding, token_id, ts_init=self._clock.timestamp_ns())
        if self._instrument_provider.find(instrument.id) is None:
            self._instrument_provider.add(instrument)
        self._handle_data(instrument)
        return instrument

    def _binding_for_token(self, token_id: str):
        for binding in self._services.bindings.values():
            if token_id in {binding.yes_token_id, binding.no_token_id}:
                return binding
        return None

    def _binding_for_instrument(self, instrument_id: InstrumentId):
        value = str(instrument_id)
        try:
            condition_id, rest = value.rsplit("-", 1)
            token_id, _venue = rest.split(".", 1)
        except ValueError:
            return None
        for binding in self._services.bindings.values():
            if binding.condition_id != condition_id:
                continue
            if token_id in {binding.yes_token_id, binding.no_token_id}:
                return binding
        return None

    @staticmethod
    def _snapshot_to_deltas(instrument, snapshot):  # type: ignore[no-untyped-def]
        deltas: list[OrderBookDelta] = [
            OrderBookDelta.clear(
                instrument_id=instrument.id,
                sequence=0,
                ts_event=int(snapshot.source_event_ts_ns or snapshot.local_receive_ts_ns),
                ts_init=int(snapshot.local_receive_ts_ns),
            )
        ]
        bid_count = len(snapshot.bids)
        ask_count = len(snapshot.asks)
        if bid_count == 0 and ask_count == 0:
            return None

        for idx, level in enumerate(snapshot.bids):
            flags = 0
            if idx == bid_count - 1 and ask_count == 0:
                flags = RecordFlag.F_LAST
            deltas.append(
                OrderBookDelta(
                    instrument_id=instrument.id,
                    action=BookAction.ADD,
                    order=BookOrder(
                        side=OrderSide.BUY,
                        price=instrument.make_price(level.price),
                        size=instrument.make_qty(level.size),
                        order_id=0,
                    ),
                    flags=flags,
                    sequence=0,
                    ts_event=int(snapshot.source_event_ts_ns or snapshot.local_receive_ts_ns),
                    ts_init=int(snapshot.local_receive_ts_ns),
                )
            )

        for idx, level in enumerate(snapshot.asks):
            flags = RecordFlag.F_LAST if idx == ask_count - 1 else 0
            deltas.append(
                OrderBookDelta(
                    instrument_id=instrument.id,
                    action=BookAction.ADD,
                    order=BookOrder(
                        side=OrderSide.SELL,
                        price=instrument.make_price(level.price),
                        size=instrument.make_qty(level.size),
                        order_id=0,
                    ),
                    flags=flags,
                    sequence=0,
                    ts_event=int(snapshot.source_event_ts_ns or snapshot.local_receive_ts_ns),
                    ts_init=int(snapshot.local_receive_ts_ns),
                )
            )

        return OrderBookDeltas(instrument_id=instrument.id, deltas=deltas)


class PolymarketPaperMarketDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: PolymarketPaperMarketDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> PolymarketPaperMarketDataClient:
        return PolymarketPaperMarketDataClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
            name=name,
        )


class PublicPolymarketDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ):
        from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
        from nautilus_trader.adapters.polymarket.factories import get_polymarket_instrument_provider
        from py_clob_client.client import ClobClient

        class PrewarmedPolymarketDataClient(PolymarketDataClient):
            def __init__(self, *args, preload_task: asyncio.Task[None] | None = None, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self._preload_task = preload_task

            async def _connect(self) -> None:
                if self._preload_task is not None:
                    await self._preload_task
                    self._preload_task = None
                else:
                    await self._instrument_provider.initialize()
                self._send_all_instruments_to_data_engine()

                if self._config.update_instruments_interval_mins:
                    self._update_instruments_task = self.create_task(
                        self._update_instruments(self._config.update_instruments_interval_mins),
                    )

        http_client = ClobClient(
            config.base_url_http or "https://clob.polymarket.com",
            chain_id=137,
        )
        provider = get_polymarket_instrument_provider(
            client=http_client,
            clock=clock,
            config=config.instrument_config,
        )
        preload_task = loop.create_task(provider.initialize())
        return PrewarmedPolymarketDataClient(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
            preload_task=preload_task,
        )


class CoinbaseSpotDataClient(LiveDataClient):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: CoinbaseSpotDataClientConfig,
        name: str,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name),
            venue=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._config = config
        self._running = True
        self._ws = None

    async def _connect(self) -> None:
        self.create_task(self._run(), log_msg="coinbase-stream")

    async def _disconnect(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _subscribe(self, command: SubscribeData) -> None:
        return None

    async def _unsubscribe(self, command: UnsubscribeData) -> None:
        return None

    async def _request(self, request: RequestData) -> None:
        self._log.warning(f"Ignoring unsupported request for Coinbase stream: {request}")

    async def _run(self) -> None:
        services = get_runtime_services(self._config.runtime_id)
        products = ["BTC-USD", "ETH-USD"]
        while self._running:
            services.mark_feed_disconnected("coinbase")
            services.write_state()
            try:
                async with websockets.connect(self._config.base_url, ping_interval=None) as ws:
                    self._ws = ws
                    services.mark_feed_connected("coinbase")
                    services.write_state()
                    await ws.send(
                        msgspec.json.encode(
                            {
                                "type": "subscribe",
                                "product_ids": products,
                                "channels": ["ticker", "heartbeat"],
                            }
                        ).decode()
                    )
                    async for raw in ws:
                        if isinstance(raw, str):
                            raw = raw.encode()
                        message = msgspec.json.decode(raw)
                        event = CoinbaseDataClient._parse_message(message)
                        if not event:
                            continue
                        if event["type"] == "feed_event":
                            services.mark_feed_event("coinbase", int(event["ts_ns"]))
                            continue
                        update = event["update"]
                        data = CoinbaseSpotPrice(
                            coin=update.symbol,
                            symbol=update.symbol,
                            price=float(update.price),
                            source_event_ts_ns=int(update.source_event_ts_ns),
                            local_receive_ts_ns=int(update.local_receive_ts_ns),
                            volume_24h=(
                                float(update.volume_24h)
                                if update.volume_24h is not None
                                else 0.0
                            ),
                            ts_event=int(update.source_event_ts_ns),
                            ts_init=int(update.local_receive_ts_ns),
                        )
                        self._handle_data(CustomData(DataType(CoinbaseSpotPrice), data))
            except Exception as exc:  # noqa: BLE001
                services.mark_feed_disconnected("coinbase", detail=str(exc))
                services.write_state(force=True)
                if self._running:
                    await asyncio.sleep(self._config.reconnect_delay_secs)


class CoinbaseSpotDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: CoinbaseSpotDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> CoinbaseSpotDataClient:
        return CoinbaseSpotDataClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
            name=name,
        )


class RtdsChainlinkDataClient(LiveDataClient):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: RtdsChainlinkDataClientConfig,
        name: str,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name),
            venue=None,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._config = config
        self._running = True
        self._connected_symbols: set[str] = set()

    async def _connect(self) -> None:
        self.create_task(self._run(), log_msg="rtds-stream")

    async def _disconnect(self) -> None:
        self._running = False

    async def _subscribe(self, command: SubscribeData) -> None:
        return None

    async def _unsubscribe(self, command: UnsubscribeData) -> None:
        return None

    async def _request(self, request: RequestData) -> None:
        self._log.warning(f"Ignoring unsupported request for RTDS stream: {request}")

    async def _run(self) -> None:
        tasks = [
            self.create_task(self._run_symbol(symbol), log_msg=f"rtds-{symbol}")
            for symbol in ("btc/usd", "eth/usd")
        ]
        await asyncio.gather(*tasks)

    async def _ping_loop(self, ws) -> None:  # type: ignore[no-untyped-def]
        while True:
            await asyncio.sleep(self._config.ping_interval_secs)
            await ws.send("PING")

    async def _run_symbol(self, symbol: str) -> None:
        services = get_runtime_services(self._config.runtime_id)
        payload = RtdsDataClient._subscription_payload(symbol)
        while self._running:
            try:
                async with websockets.connect(self._config.base_url, ping_interval=None) as ws:
                    self._mark_symbol_connected(symbol)
                    services.write_state()
                    await ws.send(payload)
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            if raw in {"PONG", ""}:
                                continue
                            if isinstance(raw, str):
                                raw = raw.encode()
                            message = msgspec.json.decode(raw)
                            event = RtdsDataClient._parse_message(message)
                            if not event:
                                continue
                            if event["type"] == "feed_event":
                                services.mark_feed_event("chainlink", int(event["ts_ns"]))
                                continue
                            update = event["update"]
                            data = ChainlinkOraclePrice(
                                coin=update.symbol,
                                symbol=update.symbol,
                                price=float(update.price),
                                source_event_ts_ns=int(update.source_event_ts_ns),
                                local_receive_ts_ns=int(update.local_receive_ts_ns),
                                ts_event=int(update.source_event_ts_ns),
                                ts_init=int(update.local_receive_ts_ns),
                            )
                            self._handle_data(CustomData(DataType(ChainlinkOraclePrice), data))
                    finally:
                        ping_task.cancel()
            except Exception as exc:  # noqa: BLE001
                self._mark_symbol_disconnected(symbol, detail=str(exc))
                services.write_state(force=True)
                if self._running:
                    await asyncio.sleep(self._config.reconnect_delay_secs)
            else:
                self._mark_symbol_disconnected(symbol, detail="")

    def _mark_symbol_connected(self, symbol: str) -> None:
        services = get_runtime_services(self._config.runtime_id)
        already_connected = bool(self._connected_symbols)
        self._connected_symbols.add(symbol)
        if not already_connected:
            services.mark_feed_connected("chainlink")

    def _mark_symbol_disconnected(self, symbol: str, *, detail: str) -> None:
        services = get_runtime_services(self._config.runtime_id)
        was_connected = bool(self._connected_symbols)
        self._connected_symbols.discard(symbol)
        if was_connected and not self._connected_symbols:
            services.mark_feed_disconnected("chainlink", detail=detail)


class RtdsChainlinkDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: RtdsChainlinkDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> RtdsChainlinkDataClient:
        return RtdsChainlinkDataClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
            name=name,
        )


class PolymarketPaperExecutionClient(LiveExecutionClient):
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: PolymarketPaperExecClientConfig,
        name: str,
    ) -> None:
        services = get_runtime_services(config.runtime_id)
        runtime = services.runtime
        super().__init__(
            loop=loop,
            client_id=ClientId(name),
            venue=POLYMARKET_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=USDC_POS,
            instrument_provider=InstrumentProvider(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._config = config
        self._runtime = runtime
        if services.paper_execution is None:
            raise ValueError("Paper execution services are not configured")
        self._paper_execution = services.paper_execution
        self._services = services
        account_id = AccountId(f"{name}-001")
        self._set_account_id(account_id)

    async def _connect(self) -> None:
        self._publish_account_state()

    async def _disconnect(self) -> None:
        return None

    def submit_order(self, command: SubmitOrder) -> None:  # type: ignore[override]
        self.generate_order_submitted(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        self.create_task(
            self._submit_order(command),
            log_msg=f"paper-submit-{command.order.client_order_id}",
        )

    def submit_order_list(self, command: SubmitOrderList) -> None:  # type: ignore[override]
        for order in command.order_list.orders:
            self.submit_order(
                SubmitOrder(
                    trader_id=command.trader_id,
                    strategy_id=command.strategy_id,
                    order=order,
                    command_id=command.command_id,
                    ts_init=command.ts_init,
                    position_id=command.position_id,
                    client_id=command.client_id,
                    params=command.params,
                )
            )

    def modify_order(self, command: ModifyOrder) -> None:  # type: ignore[override]
        self.generate_order_rejected(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            reason="paper execution client does not support modify",
            ts_event=self._clock.timestamp_ns(),
        )

    def cancel_order(self, command: CancelOrder) -> None:  # type: ignore[override]
        venue_order_id = VenueOrderId(command.client_order_id.value)
        self.generate_order_canceled(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

    def cancel_all_orders(self, command: CancelAllOrders) -> None:  # type: ignore[override]
        return None

    async def _submit_order(self, command: SubmitOrder) -> None:
        params = dict(command.params or {})
        payload = params.get("entry_intent")
        if not isinstance(payload, dict):
            self.generate_order_denied(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.order.client_order_id,
                reason="missing entry intent payload",
                ts_event=self._clock.timestamp_ns(),
            )
            return

        intent = EntryIntent(**payload)
        timing = self._paper_execution.sample_timing(intent.decision_ts_ns)
        submit_delay_secs = max(
            0.0,
            (timing.submit_ts_ns - self._clock.timestamp_ns()) / 1_000_000_000,
        )
        if submit_delay_secs > 0:
            await asyncio.sleep(submit_delay_secs)
        lifecycle = self._paper_execution.execute_intent(
            intent,
            book_timeline=self._services.timeline,
            timing=timing,
        )
        lifecycle = replace(
            lifecycle,
            metadata={
                **lifecycle.metadata,
                "client_order_id": command.order.client_order_id.value,
                "instrument_id": command.instrument_id.value,
            },
        )
        self._services.record_order_lifecycle(lifecycle)

        venue_order_id = VenueOrderId(lifecycle.order_id)
        if lifecycle.status == ExecutionStatus.REJECTED:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.order.client_order_id,
                reason=lifecycle.reason,
                ts_event=self._clock.timestamp_ns(),
            )
            self._services.write_state(force=True)
            return

        self.generate_order_accepted(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        instrument = self._cache.instrument(command.instrument_id)
        last_qty = instrument.make_qty(lifecycle.fill.filled_shares)
        last_px = instrument.make_price(lifecycle.fill.average_price)
        commission = Money(lifecycle.fill.trade_fee_usd + lifecycle.fill.gas_fee_usd, USDC_POS)
        self.generate_order_filled(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.order.client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,
            trade_id=TradeId(uuid.uuid4().hex[:16]),
            order_side=command.order.side,
            order_type=command.order.order_type,
            last_qty=last_qty,
            last_px=last_px,
            quote_currency=USDC_POS,
            commission=commission,
            liquidity_side=LiquiditySide.TAKER,
            ts_event=self._clock.timestamp_ns(),
            info=lifecycle.metadata,
        )
        if lifecycle.status == ExecutionStatus.PARTIAL:
            self.generate_order_expired(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.order.client_order_id,
                venue_order_id=venue_order_id,
                ts_event=self._clock.timestamp_ns(),
            )
        self._publish_account_state()
        self._services.write_state(force=True)

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        for lifecycle in self._services.order_lifecycles:
            if command.client_order_id is not None and (
                lifecycle.metadata.get("client_order_id") != command.client_order_id.value
            ):
                continue
            if (
                command.venue_order_id is not None
                and lifecycle.order_id != command.venue_order_id.value
            ):
                continue
            report = self._order_status_report_for(lifecycle)
            if report is not None:
                return report
        return None

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        reports: list[OrderStatusReport] = []
        for lifecycle in self._services.order_lifecycles:
            report = self._order_status_report_for(lifecycle)
            if report is None:
                continue
            if command.instrument_id is not None and report.instrument_id != command.instrument_id:
                continue
            reports.append(report)
        return reports

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        reports: list[FillReport] = []
        for lifecycle in self._services.order_lifecycles:
            report = self._fill_report_for(lifecycle)
            if report is None:
                continue
            if command.instrument_id is not None and report.instrument_id != command.instrument_id:
                continue
            if (
                command.venue_order_id is not None
                and report.venue_order_id != command.venue_order_id
            ):
                continue
            reports.append(report)
        return reports

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        reports: list[PositionStatusReport] = []
        for market_id, position in self._paper_execution.portfolio.positions.items():
            binding = self._services.bindings.get(market_id) or self._services.registry.get(
                market_id,
            )
            if binding is None:
                continue
            token_id = binding.yes_token_id if position.yes_shares > 0 else binding.no_token_id
            shares = position.yes_shares if position.yes_shares > 0 else position.no_shares
            if shares <= 0.0:
                continue
            instrument_id = polymarket_instrument_id(binding.condition_id, token_id)
            if command.instrument_id is not None and instrument_id != command.instrument_id:
                continue
            instrument = self._cache.instrument(instrument_id)
            if instrument is None:
                continue
            avg_px_open = (
                Decimal(str(position.cost_basis_usd / shares))
                if position.cost_basis_usd > 0.0 and shares > 0.0
                else None
            )
            reports.append(
                PositionStatusReport(
                    account_id=self.account_id,
                    instrument_id=instrument_id,
                    position_side=PositionSide.LONG,
                    quantity=instrument.make_qty(shares),
                    report_id=UUID4(),
                    ts_last=self._clock.timestamp_ns(),
                    ts_init=self._clock.timestamp_ns(),
                    avg_px_open=avg_px_open,
                )
            )
        return reports

    def _publish_account_state(self) -> None:
        snapshot = self._paper_execution.portfolio.snapshot()
        balance = AccountBalance(
            total=Money(snapshot.cash_balance_usd, USDC_POS),
            locked=Money(0, USDC_POS),
            free=Money(snapshot.cash_balance_usd, USDC_POS),
        )
        self.generate_account_state(
            balances=[balance],
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
            info={
                "cash_balance_usd": snapshot.cash_balance_usd,
                "open_positions": snapshot.open_positions,
            },
        )

    def _order_status_report_for(self, lifecycle: OrderLifecycle) -> OrderStatusReport | None:
        instrument_id = self._instrument_id_for_lifecycle(lifecycle)
        if instrument_id is None:
            return None
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return None

        status = {
            ExecutionStatus.FILLED: OrderStatus.FILLED,
            ExecutionStatus.PARTIAL: OrderStatus.EXPIRED,
            ExecutionStatus.REJECTED: OrderStatus.REJECTED,
        }[lifecycle.status]
        ts_last = (
            lifecycle.confirmed_ts_ns
            or lifecycle.fill_ts_ns
            or lifecycle.ack_ts_ns
            or lifecycle.submit_ts_ns
        )
        client_order_id = lifecycle.metadata.get("client_order_id")
        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            venue_order_id=VenueOrderId(lifecycle.order_id),
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.IOC,
            order_status=status,
            quantity=instrument.make_qty(lifecycle.requested_shares),
            filled_qty=instrument.make_qty(lifecycle.fill.filled_shares),
            report_id=UUID4(),
            ts_accepted=lifecycle.ack_ts_ns or lifecycle.submit_ts_ns,
            ts_last=ts_last,
            ts_init=self._clock.timestamp_ns(),
            client_order_id=(
                ClientOrderId(str(client_order_id))
                if client_order_id is not None
                else None
            ),
            price=instrument.make_price(lifecycle.limit_price),
            avg_px=(
                Decimal(str(lifecycle.fill.average_price))
                if lifecycle.fill.average_price > 0.0
                else None
            ),
        )

    def _fill_report_for(self, lifecycle: OrderLifecycle) -> FillReport | None:
        if lifecycle.fill.filled_shares <= 0.0:
            return None
        instrument_id = self._instrument_id_for_lifecycle(lifecycle)
        if instrument_id is None:
            return None
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return None

        client_order_id = lifecycle.metadata.get("client_order_id")
        return FillReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            venue_order_id=VenueOrderId(lifecycle.order_id),
            trade_id=TradeId(uuid.uuid5(uuid.NAMESPACE_URL, lifecycle.order_id).hex[:16]),
            order_side=OrderSide.BUY,
            last_qty=instrument.make_qty(lifecycle.fill.filled_shares),
            last_px=instrument.make_price(lifecycle.fill.average_price),
            commission=Money(
                lifecycle.fill.trade_fee_usd + lifecycle.fill.gas_fee_usd,
                USDC_POS,
            ),
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=(
                lifecycle.fill_ts_ns
                or lifecycle.ack_ts_ns
                or lifecycle.submit_ts_ns
            ),
            ts_init=self._clock.timestamp_ns(),
            client_order_id=(
                ClientOrderId(str(client_order_id))
                if client_order_id is not None
                else None
            ),
        )

    def _instrument_id_for_lifecycle(self, lifecycle: OrderLifecycle) -> InstrumentId | None:
        instrument_id = lifecycle.metadata.get("instrument_id")
        if instrument_id is not None:
            return InstrumentId.from_str(str(instrument_id))
        binding = self._services.bindings.get(lifecycle.market_id) or self._services.registry.get(
            lifecycle.market_id,
        )
        if binding is None:
            return None
        return polymarket_instrument_id(binding.condition_id, lifecycle.token_id)


class PolymarketPaperExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: PolymarketPaperExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> PolymarketPaperExecutionClient:
        return PolymarketPaperExecutionClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
            name=name,
        )
