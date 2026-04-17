from nautilus_trader.model.data import CustomData, DataType

from cbcl_platform.live.coinbase_data_client import CoinbaseDataClient
from cbcl_platform.live.rtds_data_client import RtdsDataClient
from cbcl_platform.nautilus.data import ChainlinkOraclePrice, CoinbaseSpotPrice


def test_coinbase_parse_ticker_and_heartbeat() -> None:
    heartbeat = CoinbaseDataClient._parse_message({"type": "heartbeat"})
    assert heartbeat is not None
    assert heartbeat["type"] == "feed_event"
    assert heartbeat["feed"] == "coinbase"

    ticker = CoinbaseDataClient._parse_message(
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "74250.11",
            "volume_24h": "18234.55",
            "time": "2026-04-15T05:00:00.000Z",
        },
    )
    assert ticker is not None
    assert ticker["type"] == "coinbase_price"
    assert ticker["coin"] == "BTC"
    assert ticker["update"].price == 74250.11
    assert ticker["update"].volume_24h == 18234.55


def test_rtds_parse_update_message() -> None:
    parsed = RtdsDataClient._parse_message(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 1776231034000,
                "value": 2325.12,
            },
        },
    )

    assert parsed is not None
    assert parsed["type"] == "chainlink_price"
    assert parsed["coin"] == "ETH"
    assert parsed["update"].price == 2325.12


def test_rtds_parse_subscribe_snapshot_message() -> None:
    parsed = RtdsDataClient._parse_message(
        {
            "topic": "crypto_prices",
            "type": "subscribe",
            "payload": {
                "symbol": "btc/usd",
                "data": [
                    {"timestamp": 1776231034000, "value": 74250.11},
                    {"timestamp": 1776231035000, "value": 74251.22},
                ],
            },
        },
    )

    assert parsed is not None
    assert parsed["type"] == "chainlink_price"
    assert parsed["coin"] == "BTC"
    assert parsed["update"].price == 74251.22


def test_rtds_non_update_is_health_event() -> None:
    parsed = RtdsDataClient._parse_message(
        {
            "topic": "crypto_prices_chainlink",
            "type": "subscribed",
            "payload": {},
        },
    )

    assert parsed is not None
    assert parsed["type"] == "feed_event"
    assert parsed["feed"] == "chainlink"


def test_rtds_subscription_payloads_split_symbols() -> None:
    payloads = RtdsDataClient._subscription_payloads()

    assert len(payloads) == 2
    assert '\\"symbol\\":\\"btc/usd\\"' in payloads[0]
    assert '\\"symbol\\":\\"eth/usd\\"' in payloads[1]


def test_nautilus_custom_data_accepts_timestamps() -> None:
    spot = CoinbaseSpotPrice(
        coin="BTC",
        symbol="BTC",
        price=74250.11,
        source_event_ts_ns=1,
        local_receive_ts_ns=2,
        volume_24h=100.0,
        ts_event=1,
        ts_init=2,
    )
    oracle = ChainlinkOraclePrice(
        coin="ETH",
        symbol="ETH",
        price=2325.12,
        source_event_ts_ns=3,
        local_receive_ts_ns=4,
        ts_event=3,
        ts_init=4,
    )

    assert spot.ts_event == 1
    assert spot.ts_init == 2
    assert oracle.ts_event == 3
    assert oracle.ts_init == 4
    wrapped = CustomData(DataType(CoinbaseSpotPrice), spot)
    assert wrapped.data is spot
