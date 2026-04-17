from __future__ import annotations

from dataclasses import dataclass, field
from os import getenv

from cbcl_platform.constants import POLYMARKET_MIN_SHARES_PER_ORDER


def _get_bool(name: str, default: bool) -> bool:
    raw = getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_float(name: str, default: float) -> float:
    raw = getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _get_int(name: str, default: int) -> int:
    raw = getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _get_csv(name: str, default: str) -> tuple[str, ...]:
    raw = getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class StrategyConfig:
    strategy_name: str = "cb_cl_005"
    threshold: float = 0.0005
    max_minutes_to_close_5m: float = 2.0
    max_minutes_to_close_15m: float = 2.0
    entry_window_grace_minutes: float = 0.25
    min_buy_price: float = 0.60
    max_buy_price: float = 0.90
    max_price_drift: float = 0.02
    signal_confidence: float = 0.95


@dataclass(frozen=True)
class FreshnessConfig:
    polymarket_book_age_ms: int = 12_000
    coinbase_age_ms: int = 12_000
    chainlink_age_ms: int = 12_000
    max_feed_skew_ms: int = 0
    decision_submit_budget_ms: int = 250


@dataclass(frozen=True)
class KellyConfig:
    enabled: bool = True
    fraction: float = 0.25
    max_bankroll_fraction: float = 0.05
    confidence_blend: float = 0.8
    min_trades_for_full_trust: int = 40
    bootstrap_fixed_shares_enabled: bool = True
    bootstrap_enable_balance_usd: float = 150.0
    bootstrap_disable_balance_usd: float = 125.0
    min_shares: float = POLYMARKET_MIN_SHARES_PER_ORDER


@dataclass(frozen=True)
class DelayPercentiles:
    p50_ms: int
    p95_ms: int
    p99_ms: int


@dataclass(frozen=True)
class PaperExecutionConfig:
    initial_balance_usd: float = 1_000.0
    internal_delay: DelayPercentiles = field(
        default_factory=lambda: DelayPercentiles(p50_ms=2, p95_ms=8, p99_ms=15),
    )
    signing_delay: DelayPercentiles = field(
        default_factory=lambda: DelayPercentiles(p50_ms=1_000, p95_ms=1_700, p99_ms=2_500),
    )
    submit_rtt: DelayPercentiles = field(
        default_factory=lambda: DelayPercentiles(p50_ms=120, p95_ms=300, p99_ms=700),
    )
    ack_delay: DelayPercentiles = field(
        default_factory=lambda: DelayPercentiles(p50_ms=80, p95_ms=250, p99_ms=500),
    )
    matched_to_confirmed_min_ms: int = 2_000
    matched_to_confirmed_max_ms: int = 5_000
    slow_submit_probability: float = 0.02
    slow_submit_extra_min_ms: int = 500
    slow_submit_extra_max_ms: int = 2_000
    matching_engine_restart_block_ms: int = 90_000
    loop_interval_ms: int = 250
    state_flush_interval_ms: int = 500
    market_duration_5m_s: int = 300
    market_duration_15m_s: int = 900
    signal_cooldown_ms: int = 1_500
    random_seed: int = 42


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class MarketRegistryConfig:
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    active_refresh_interval_s: int = 15
    max_active_markets: int = 400
    max_markets_per_slot: int = 2


@dataclass(frozen=True)
class PolymarketMarketWsConfig:
    base_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"
    ws_connection_initial_delay_secs: float = 0.25
    ws_connection_delay_secs: float = 0.1
    ws_max_subscriptions_per_connection: int = 200


@dataclass(frozen=True)
class CoinbaseWsConfig:
    base_url: str = "wss://ws-feed.exchange.coinbase.com"
    reconnect_delay_secs: float = 1.0
    ping_interval_secs: float = 15.0


@dataclass(frozen=True)
class RtdsWsConfig:
    base_url: str = "wss://ws-live-data.polymarket.com"
    reconnect_delay_secs: float = 1.0
    ping_interval_secs: float = 5.0


@dataclass(frozen=True)
class RecorderConfig:
    path: str = "./data/replay.jsonl"
    enabled: bool = True


@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    markets: tuple[str, ...]
    redis_url: str
    postgres_dsn: str
    runtime_state_path: str
    strategy: StrategyConfig
    freshness: FreshnessConfig
    kelly: KellyConfig
    paper_execution: PaperExecutionConfig
    dashboard: DashboardConfig
    market_registry: MarketRegistryConfig
    polymarket_market_ws: PolymarketMarketWsConfig
    coinbase_ws: CoinbaseWsConfig
    rtds_ws: RtdsWsConfig
    recorder: RecorderConfig

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        return cls(
            environment=getenv("CBCL_ENV", "dev"),
            markets=_get_csv("CBCL_MARKETS", "BTC,ETH"),
            redis_url=getenv("CBCL_REDIS_URL", "redis://localhost:6379/0"),
            postgres_dsn=getenv(
                "CBCL_POSTGRES_DSN",
                "postgresql://cbcl:cbcl@localhost:5432/cbcl",
            ),
            runtime_state_path=getenv(
                "CBCL_RUNTIME_STATE_PATH",
                "./data/runtime_state.json",
            ),
            strategy=StrategyConfig(
                strategy_name=getenv("CBCL_STRATEGY_NAME", "cb_cl_005"),
                threshold=_get_float("CBCL_THRESHOLD", 0.0005),
                max_minutes_to_close_5m=_get_float("CBCL_MAX_MINUTES_TO_CLOSE_5M", 2.0),
                max_minutes_to_close_15m=_get_float("CBCL_MAX_MINUTES_TO_CLOSE_15M", 2.0),
                entry_window_grace_minutes=_get_float("CBCL_ENTRY_WINDOW_GRACE_MINUTES", 0.25),
                min_buy_price=_get_float("CBCL_MIN_BUY_PRICE", 0.60),
                max_buy_price=_get_float("CBCL_MAX_BUY_PRICE", 0.90),
                max_price_drift=_get_float("CBCL_MAX_PRICE_DRIFT", 0.02),
                signal_confidence=_get_float("CBCL_SIGNAL_CONFIDENCE", 0.95),
            ),
            freshness=FreshnessConfig(
                polymarket_book_age_ms=_get_int("CBCL_BOOK_MAX_AGE_MS", 12_000),
                coinbase_age_ms=_get_int("CBCL_COINBASE_MAX_AGE_MS", 12_000),
                chainlink_age_ms=_get_int("CBCL_CHAINLINK_MAX_AGE_MS", 12_000),
                max_feed_skew_ms=_get_int("CBCL_MAX_FEED_SKEW_MS", 0),
                decision_submit_budget_ms=_get_int("CBCL_DECISION_SUBMIT_BUDGET_MS", 250),
            ),
            kelly=KellyConfig(
                enabled=_get_bool("CBCL_KELLY_ENABLED", True),
                fraction=_get_float("CBCL_KELLY_FRACTION", 0.25),
                max_bankroll_fraction=_get_float("CBCL_KELLY_MAX_BANKROLL_FRACTION", 0.05),
                confidence_blend=_get_float("CBCL_KELLY_CONFIDENCE_BLEND", 0.8),
                min_trades_for_full_trust=_get_int("CBCL_KELLY_MIN_TRADES_FOR_FULL_TRUST", 40),
                bootstrap_fixed_shares_enabled=_get_bool(
                    "CBCL_KELLY_BOOTSTRAP_FIXED_SHARES_ENABLED",
                    True,
                ),
                bootstrap_enable_balance_usd=_get_float(
                    "CBCL_KELLY_BOOTSTRAP_ENABLE_BALANCE_USD",
                    150.0,
                ),
                bootstrap_disable_balance_usd=_get_float(
                    "CBCL_KELLY_BOOTSTRAP_DISABLE_BALANCE_USD",
                    125.0,
                ),
                min_shares=_get_float("CBCL_MIN_SHARES", POLYMARKET_MIN_SHARES_PER_ORDER),
            ),
            paper_execution=PaperExecutionConfig(
                initial_balance_usd=_get_float("CBCL_PAPER_INITIAL_BALANCE_USD", 1_000.0),
                loop_interval_ms=_get_int("CBCL_PAPER_LOOP_INTERVAL_MS", 250),
                state_flush_interval_ms=_get_int("CBCL_PAPER_STATE_FLUSH_INTERVAL_MS", 500),
                market_duration_5m_s=_get_int("CBCL_PAPER_MARKET_DURATION_5M_S", 300),
                market_duration_15m_s=_get_int("CBCL_PAPER_MARKET_DURATION_15M_S", 900),
                signal_cooldown_ms=_get_int("CBCL_PAPER_SIGNAL_COOLDOWN_MS", 1_500),
                random_seed=_get_int("CBCL_PAPER_RANDOM_SEED", 42),
            ),
            dashboard=DashboardConfig(
                host=getenv("CBCL_DASHBOARD_HOST", "127.0.0.1"),
                port=_get_int("CBCL_DASHBOARD_PORT", 8080),
            ),
            market_registry=MarketRegistryConfig(
                gamma_base_url=getenv(
                    "CBCL_POLYMARKET_GAMMA_URL",
                    "https://gamma-api.polymarket.com",
                ),
                active_refresh_interval_s=_get_int("CBCL_DISCOVERY_REFRESH_S", 15),
                max_active_markets=_get_int("CBCL_DISCOVERY_MAX_ACTIVE_MARKETS", 400),
                max_markets_per_slot=_get_int("CBCL_DISCOVERY_MAX_PER_SLOT", 2),
            ),
            polymarket_market_ws=PolymarketMarketWsConfig(
                base_url=getenv(
                    "CBCL_POLYMARKET_MARKET_WS_URL",
                    "wss://ws-subscriptions-clob.polymarket.com/ws/",
                ),
                ws_connection_initial_delay_secs=_get_float(
                    "CBCL_POLYMARKET_WS_INITIAL_DELAY_S",
                    0.25,
                ),
                ws_connection_delay_secs=_get_float(
                    "CBCL_POLYMARKET_WS_DELAY_S",
                    0.1,
                ),
                ws_max_subscriptions_per_connection=_get_int(
                    "CBCL_POLYMARKET_WS_MAX_SUBS",
                    200,
                ),
            ),
            coinbase_ws=CoinbaseWsConfig(
                base_url=getenv(
                    "CBCL_COINBASE_WS_URL",
                    "wss://ws-feed.exchange.coinbase.com",
                ),
                reconnect_delay_secs=_get_float("CBCL_COINBASE_RECONNECT_S", 1.0),
                ping_interval_secs=_get_float("CBCL_COINBASE_PING_S", 15.0),
            ),
            rtds_ws=RtdsWsConfig(
                base_url=getenv(
                    "CBCL_RTDS_WS_URL",
                    "wss://ws-live-data.polymarket.com",
                ),
                reconnect_delay_secs=_get_float("CBCL_RTDS_RECONNECT_S", 1.0),
                ping_interval_secs=_get_float("CBCL_RTDS_PING_S", 5.0),
            ),
            recorder=RecorderConfig(
                path=getenv("CBCL_RECORDER_PATH", "./data/replay.jsonl"),
                enabled=_get_bool("CBCL_RECORDER_ENABLED", True),
            ),
        )
