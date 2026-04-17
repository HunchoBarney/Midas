from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from nautilus_trader.model.currencies import USDC_POS
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price, Quantity

from cbcl_platform.models import LiveMarketBinding
from cbcl_platform.nautilus.polymarket_ids import polymarket_instrument_id


@customdataclass
class CoinbaseSpotPrice(Data):
    coin: str
    symbol: str
    price: float
    source_event_ts_ns: int
    local_receive_ts_ns: int
    volume_24h: float = 0.0


@customdataclass
class ChainlinkOraclePrice(Data):
    coin: str
    symbol: str
    price: float
    source_event_ts_ns: int
    local_receive_ts_ns: int


def binding_instrument_ids(
    bindings: Iterable[LiveMarketBinding],
) -> frozenset[InstrumentId]:
    instrument_ids: set[InstrumentId] = set()
    for binding in bindings:
        instrument_ids.add(
            polymarket_instrument_id(
                binding.condition_id,
                binding.yes_token_id,
            ),
        )
        instrument_ids.add(
            polymarket_instrument_id(
                binding.condition_id,
                binding.no_token_id,
            ),
        )
    return frozenset(instrument_ids)


def make_polymarket_instrument(
    binding: LiveMarketBinding,
    token_id: str,
    *,
    ts_init: int | None = None,
) -> BinaryOption:
    if token_id == binding.yes_token_id:
        outcome = "YES"
    elif token_id == binding.no_token_id:
        outcome = "NO"
    else:
        raise ValueError(f"Token {token_id} is not part of binding {binding.market_id}")

    ts_value = ts_init if ts_init is not None else int(datetime.now(UTC).timestamp() * 1e9)
    expiration_ns = int(binding.expires_at_ns)
    price_increment = Price.from_str("0.001")
    size_increment = Quantity.from_str("0.000001")
    min_quantity = Quantity.from_str("0.01")

    return BinaryOption(
        instrument_id=polymarket_instrument_id(binding.condition_id, token_id),
        raw_symbol=Symbol(token_id),
        outcome=outcome,
        description=binding.event_slug,
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC_POS,
        price_increment=price_increment,
        price_precision=price_increment.precision,
        size_increment=size_increment,
        size_precision=size_increment.precision,
        activation_ns=0,
        expiration_ns=expiration_ns,
        max_quantity=None,
        min_quantity=min_quantity,
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0"),
        ts_event=ts_value,
        ts_init=ts_value,
        info={
            "market_id": binding.market_id,
            "condition_id": binding.condition_id,
            "event_slug": binding.event_slug,
            "coin": binding.coin,
            "interval": binding.interval.value,
            "token_id": token_id,
        },
    )
