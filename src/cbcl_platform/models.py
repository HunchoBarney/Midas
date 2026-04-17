from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cbcl_platform.constants import clamp_binary_price


class RuntimeMode(StrEnum):
    LIVE = "live"
    PAPER = "paper"
    DASHBOARD = "dashboard"
    REPLAY = "replay"
    BACKTEST = "backtest"


class ContractInterval(StrEnum):
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    HOURLY = "1h"


class OutcomeSide(StrEnum):
    YES = "YES"
    NO = "NO"


class MarketDirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class ExecutionAction(StrEnum):
    SUBMIT = "submit"
    REJECT = "reject"


class ExecutionStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class PriceUpdate:
    source: str
    symbol: str
    price: float
    source_event_ts_ns: int
    local_receive_ts_ns: int
    volume_24h: float | None = None

    def age_ms(self, now_ns: int) -> float:
        return max(0.0, (now_ns - self.local_receive_ts_ns) / 1_000_000.0)


@dataclass(frozen=True)
class BookFill:
    filled_shares: float
    avg_price: float
    total_cost: float


@dataclass(frozen=True)
class OrderBookSnapshot:
    token_id: str
    asks: tuple[OrderBookLevel, ...]
    bids: tuple[OrderBookLevel, ...] = ()
    source_event_ts_ns: int | None = None
    local_receive_ts_ns: int = 0

    def __post_init__(self) -> None:
        asks = tuple(
            sorted(
                (
                    OrderBookLevel(price=clamp_binary_price(level.price), size=float(level.size))
                    for level in self.asks
                    if level.price > 0.0 and level.size > 0.0
                ),
                key=lambda level: level.price,
            )
        )
        bids = tuple(
            sorted(
                (
                    OrderBookLevel(price=clamp_binary_price(level.price), size=float(level.size))
                    for level in self.bids
                    if level.price > 0.0 and level.size > 0.0
                ),
                key=lambda level: level.price,
                reverse=True,
            )
        )
        object.__setattr__(self, "asks", asks)
        object.__setattr__(self, "bids", bids)

    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def age_ms(self, now_ns: int) -> float:
        return max(0.0, (now_ns - self.local_receive_ts_ns) / 1_000_000.0)

    def shares_available_at_or_below(self, limit_price: float) -> float:
        return round(
            sum(level.size for level in self.asks if level.price <= limit_price + 1e-9),
            8,
        )

    def buy_fill(self, limit_price: float, target_shares: float) -> BookFill:
        remaining = max(0.0, float(target_shares))
        total_cost = 0.0
        filled = 0.0
        for level in self.asks:
            if level.price > limit_price + 1e-9 or remaining <= 1e-9:
                break
            take = min(level.size, remaining)
            if take <= 0.0:
                continue
            total_cost += take * level.price
            filled += take
            remaining -= take
        avg_price = total_cost / filled if filled > 0.0 else 0.0
        return BookFill(
            filled_shares=round(filled, 8),
            avg_price=round(avg_price, 8),
            total_cost=round(total_cost, 8),
        )


@dataclass(frozen=True)
class MarketDescriptor:
    market_id: str
    event_slug: str
    coin: str
    interval: ContractInterval
    expires_at_ns: int
    yes_token_id: str
    no_token_id: str

    def minutes_to_close(self, now_ns: int) -> float:
        return max(0.0, (self.expires_at_ns - now_ns) / 60_000_000_000.0)


@dataclass(frozen=True)
class StrategyMarketState:
    market: MarketDescriptor
    coinbase_price: PriceUpdate | None
    chainlink_price: PriceUpdate | None
    yes_book: OrderBookSnapshot | None
    no_book: OrderBookSnapshot | None


@dataclass(frozen=True)
class LiveMarketBinding:
    market_id: str
    event_slug: str
    coin: str
    interval: ContractInterval
    expires_at_ns: int
    yes_token_id: str
    no_token_id: str
    condition_id: str
    resolved: bool = False
    winning_token_id: str | None = None
    resolution_source: str | None = None

    def to_market_descriptor(self) -> MarketDescriptor:
        return MarketDescriptor(
            market_id=self.market_id,
            event_slug=self.event_slug,
            coin=self.coin,
            interval=self.interval,
            expires_at_ns=self.expires_at_ns,
            yes_token_id=self.yes_token_id,
            no_token_id=self.no_token_id,
        )


@dataclass(frozen=True)
class MarketResolution:
    market_id: str
    winning_token_id: str
    resolved_ts_ns: int
    source: str


@dataclass(frozen=True)
class FeedStatus:
    name: str
    connected: bool
    last_event_ts_ns: int | None
    reconnect_count: int = 0
    detail: str = ""

    def age_ms(self, now_ns: int) -> float | None:
        if self.last_event_ts_ns is None:
            return None
        return max(0.0, (now_ns - self.last_event_ts_ns) / 1_000_000.0)


@dataclass(frozen=True)
class RuntimeHealthSnapshot:
    feeds: dict[str, FeedStatus]
    feed_skew_ms_by_coin: dict[str, float]
    stale_reasons: dict[str, str]


@dataclass(frozen=True)
class ExecutionTelemetry:
    submit_ms: int
    ack_ms: int
    confirm_ms: int | None
    decision_age_ms: float


@dataclass(frozen=True)
class PendingPaperOrder:
    intent: EntryIntent
    submit_ts_ns: int
    ack_ts_ns: int
    confirm_delay_ms: int
    timing_metadata: dict[str, float | int]


@dataclass(frozen=True)
class KellyCalibration:
    trade_count: int = 0
    win_rate: float = 0.0


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash_balance_usd: float
    total_exposure_usd: float
    open_positions: int = 0

    @property
    def available_bankroll_usd(self) -> float:
        return max(0.0, self.cash_balance_usd)


@dataclass(frozen=True)
class KellySizingResult:
    accepted: bool
    reason: str
    target_size_usd: float
    target_shares: float
    bootstrap_mode: str
    win_prob: float
    full_kelly: float
    bet_fraction: float
    metadata: dict[str, float | int | str] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryIntent:
    strategy_name: str
    market_id: str
    token_id: str
    side: OutcomeSide
    direction: MarketDirection
    decision_ts_ns: int
    signal_price: float
    hard_cap: float
    drift_cap: float
    size_usd: float
    target_shares: float
    expected_profit_usd: float
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyDecision:
    accepted: bool
    reason: str
    intent: EntryIntent | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BookQuote:
    signal_price: float
    executable_price: float
    target_shares: float
    executable_shares: float
    target_reachable: bool
    telemetry: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionDecision:
    action: ExecutionAction
    reason: str
    limit_price: float
    expected_shares: float
    quote: BookQuote
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FillResult:
    status: ExecutionStatus
    filled_shares: float
    average_price: float
    total_cost: float
    trade_fee_usd: float
    gas_fee_usd: float


@dataclass(frozen=True)
class OrderLifecycle:
    order_id: str
    market_id: str
    token_id: str
    side: OutcomeSide
    status: ExecutionStatus
    reason: str
    decision_ts_ns: int
    submit_ts_ns: int
    ack_ts_ns: int | None
    fill_ts_ns: int | None
    confirmed_ts_ns: int | None
    limit_price: float
    requested_shares: float
    fill: FillResult
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaperPosition:
    market_id: str
    yes_shares: float = 0.0
    no_shares: float = 0.0
    cost_basis_usd: float = 0.0
    fees_paid_usd: float = 0.0


@dataclass(frozen=True)
class DashboardSnapshot:
    runtime_mode: str
    environment: str
    markets: tuple[str, ...]
    strategy_name: str
    hard_cap: float
    max_drift: float
    realistic_paper_enabled: bool
    kelly_enabled: bool
    commands: tuple[str, ...]
    notes: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
