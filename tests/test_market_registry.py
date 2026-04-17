import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from cbcl_platform.config import RuntimeConfig
from cbcl_platform.live.market_registry import (
    MarketRegistry,
    _bucket_start_seconds,
    _extract_winning_token_id,
)
from cbcl_platform.models import ContractInterval


def _sample_market(**overrides):
    market = {
        "slug": "btc-updown-5m-test",
        "question": "Will BTC be up/down in 5 minutes?",
        "clobTokenIds": '["yes-token","no-token"]',
        "outcomes": '["Yes","No"]',
        "endDateIso": "2126-04-15T00:05:00Z",
        "endDate": "2126-04-15T00:05:00Z",
        "conditionId": "condition-1",
        "active": True,
        "closed": False,
        "archived": False,
        "tokens": [
            {"tokenId": "yes-token", "winner": False},
            {"tokenId": "no-token", "winner": False},
        ],
    }
    market.update(overrides)
    return market


def test_parse_updown_market_extracts_binding() -> None:
    config = RuntimeConfig.from_env()
    registry = MarketRegistry(
        http_client=object(),  # type: ignore[arg-type]
        config=config.market_registry,
        allowed_coins=("BTC", "ETH"),
    )
    binding = registry._parse_market(_sample_market())  # noqa: SLF001

    assert binding is not None
    assert binding.coin == "BTC"
    assert binding.interval == ContractInterval.FIVE_MINUTES
    assert binding.yes_token_id == "yes-token"
    assert binding.no_token_id == "no-token"
    assert binding.market_id == "condition-1"


def test_parse_updown_market_maps_up_down_outcomes() -> None:
    config = RuntimeConfig.from_env()
    registry = MarketRegistry(
        http_client=object(),  # type: ignore[arg-type]
        config=config.market_registry,
        allowed_coins=("BTC", "ETH"),
    )
    binding = registry._parse_market(  # noqa: SLF001
        _sample_market(
            clobTokenIds='["up-token","down-token"]',
            outcomes='["Up","Down"]',
        ),
    )

    assert binding is not None
    assert binding.yes_token_id == "up-token"
    assert binding.no_token_id == "down-token"


def test_parse_market_rejects_non_updown() -> None:
    config = RuntimeConfig.from_env()
    registry = MarketRegistry(
        http_client=object(),  # type: ignore[arg-type]
        config=config.market_registry,
        allowed_coins=("BTC", "ETH"),
    )
    binding = registry._parse_market(  # noqa: SLF001
        _sample_market(
            slug="will-btc-hit-100k",
            question="Will BTC hit 100k this month?",
        ),
    )

    assert binding is None


def test_parse_hourly_updown_market_extracts_hourly_interval() -> None:
    config = RuntimeConfig.from_env()
    registry = MarketRegistry(
        http_client=object(),  # type: ignore[arg-type]
        config=config.market_registry,
        allowed_coins=("BTC", "ETH"),
    )
    binding = registry._parse_market(  # noqa: SLF001
        _sample_market(
            slug="bitcoin-up-or-down-april-16-2026-4pm-et",
            question="Bitcoin Up or Down - April 16, 4PM ET",
            endDate="2126-04-16T21:00:00Z",
            endDateIso="2126-04-16",
        ),
    )

    assert binding is not None
    assert binding.interval == ContractInterval.HOURLY
    assert binding.expires_at_ns > 0


def test_resolve_winner_from_token_payload() -> None:
    config = RuntimeConfig.from_env()
    registry = MarketRegistry(
        http_client=object(),  # type: ignore[arg-type]
        config=config.market_registry,
        allowed_coins=("BTC", "ETH"),
    )
    binding = registry._parse_market(_sample_market())  # noqa: SLF001
    assert binding is not None

    winner = _extract_winning_token_id(
        _sample_market(
            tokens=[
                {"tokenId": "yes-token", "winner": True},
                {"tokenId": "no-token", "winner": False},
            ],
        ),
        binding,
    )

    assert winner == binding.yes_token_id


def test_resolve_winner_from_up_down_outcome_prices() -> None:
    config = RuntimeConfig.from_env()
    registry = MarketRegistry(
        http_client=object(),  # type: ignore[arg-type]
        config=config.market_registry,
        allowed_coins=("BTC", "ETH"),
    )
    binding = registry._parse_market(  # noqa: SLF001
        _sample_market(
            clobTokenIds='["up-token","down-token"]',
            outcomes='["Up","Down"]',
        ),
    )
    assert binding is not None

    winner = _extract_winning_token_id(
        _sample_market(
            clobTokenIds='["up-token","down-token"]',
            outcomes='["Up","Down"]',
            outcomePrices='["1.0","0.0"]',
            tokens=None,
        ),
        binding,
    )

    assert winner == binding.yes_token_id


def test_refresh_pages_through_gamma_results() -> None:
    page1 = [
        {
            "slug": "will-btc-hit-100k",
            "question": "Will BTC hit 100k this month?",
            "conditionId": "irrelevant-1",
        },
        {
            "slug": "will-eth-hit-10k",
            "question": "Will ETH hit 10k this month?",
            "conditionId": "irrelevant-2",
        },
    ]
    page2 = [_sample_market(conditionId="condition-2")]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json=page1)
        if offset == 2:
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=[])

    config = replace(RuntimeConfig.from_env().market_registry, max_active_markets=2)
    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            registry = MarketRegistry(
                http_client=client,
                config=config,
                allowed_coins=("BTC", "ETH"),
            )
            return await registry.refresh()

    markets = asyncio.run(run())

    assert "condition-2" in markets


def test_refresh_filters_expired_markets() -> None:
    expired = _sample_market(
        conditionId="expired",
        endDate="2020-01-01T00:00:00Z",
        endDateIso="2020-01-01",
    )
    future = _sample_market(
        conditionId="future",
        endDate="2126-04-15T00:05:00Z",
        endDateIso="2126-04-15",
    )

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=[expired, future])
            )
        ) as client:
            registry = MarketRegistry(
                http_client=client,
                config=RuntimeConfig.from_env().market_registry,
                allowed_coins=("BTC", "ETH"),
            )
            return await registry.refresh()

    markets = asyncio.run(run())

    assert "future" in markets
    assert "expired" not in markets


def test_refresh_limits_selected_markets_per_slot() -> None:
    markets = [
        _sample_market(
            conditionId=f"condition-{index}",
            slug=f"btc-updown-5m-{index}",
            endDate=f"2126-04-15T00:{5 + index:02d}:00Z",
            endDateIso=f"2126-04-15T00:{5 + index:02d}:00Z",
        )
        for index in range(4)
    ]

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=markets))
        ) as client:
            registry = MarketRegistry(
                http_client=client,
                config=replace(
                    RuntimeConfig.from_env().market_registry,
                    max_markets_per_slot=2,
                ),
                allowed_coins=("BTC", "ETH"),
            )
            return await registry.refresh()

    bindings = asyncio.run(run())

    assert len(bindings) == 2
    assert set(bindings) == {"condition-0", "condition-1"}


def test_refresh_prefers_current_and_next_actionable_markets_per_slot() -> None:
    now = datetime.now(UTC)
    bucket_start = _bucket_start_seconds(
        now_ns=int(now.timestamp() * 1_000_000_000),
        interval=ContractInterval.FIVE_MINUTES,
    )
    current_end = datetime.fromtimestamp(bucket_start + 300, UTC).isoformat().replace(
        "+00:00",
        "Z",
    )
    next_end = datetime.fromtimestamp(bucket_start + 600, UTC).isoformat().replace(
        "+00:00",
        "Z",
    )
    markets = [
        _sample_market(
            conditionId="current-5m",
            slug=f"btc-updown-5m-{bucket_start}",
            endDate=current_end,
            endDateIso=current_end,
        ),
        _sample_market(
            conditionId="next-5m",
            slug=f"btc-updown-5m-{bucket_start + 300}",
            endDate=next_end,
            endDateIso=next_end,
        ),
        _sample_market(
            conditionId="far-5m",
            slug=f"btc-updown-5m-{bucket_start + 10800}",
            endDate=(now + timedelta(minutes=160)).isoformat().replace("+00:00", "Z"),
            endDateIso=(now + timedelta(minutes=160)).isoformat().replace("+00:00", "Z"),
        ),
    ]

    market_by_slug = {market["slug"]: market for market in markets}

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=(
                        [market_by_slug[request.url.params["slug"]]]
                        if request.url.params.get("slug") in market_by_slug
                        else markets
                    ),
                )
            )
        ) as client:
            registry = MarketRegistry(
                http_client=client,
                config=replace(RuntimeConfig.from_env().market_registry, max_markets_per_slot=2),
                allowed_coins=("BTC", "ETH"),
            )
            return await registry.refresh()

    bindings = asyncio.run(run())

    assert set(bindings) == {"current-5m", "next-5m"}


def test_refresh_uses_public_search_fast_path() -> None:
    now = datetime.now(UTC)
    btc_market = _sample_market(
        slug="btc-updown-5m-fast-btc",
        question="Bitcoin Up or Down - April 16, 4:00PM-4:05PM ET",
        clobTokenIds='["up-token","down-token"]',
        outcomes='["Up","Down"]',
        endDate=(now + timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
        endDateIso=(now + timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
        conditionId="fast-btc",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/public-search"):
            return httpx.Response(200, json={"events": [{"markets": [btc_market]}]})
        return httpx.Response(200, json=[])

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            registry = MarketRegistry(
                http_client=client,
                config=RuntimeConfig.from_env().market_registry,
                allowed_coins=("BTC", "ETH"),
            )
            return await registry.refresh()

    markets = asyncio.run(run())

    assert "fast-btc" in markets


def test_refresh_falls_back_when_public_search_only_has_far_future_markets() -> None:
    now = datetime.now(UTC)
    far_future = _sample_market(
        conditionId="search-far",
        slug="btc-updown-5m-search-far",
        endDate=(now + timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
        endDateIso=(now + timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
    )
    near_market = _sample_market(
        conditionId="near-market",
        slug="btc-updown-5m-near",
        endDate=(now + timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
        endDateIso=(now + timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/public-search"):
            return httpx.Response(200, json={"events": [{"markets": [far_future]}]})
        return httpx.Response(200, json=[near_market])

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            registry = MarketRegistry(
                http_client=client,
                config=RuntimeConfig.from_env().market_registry,
                allowed_coins=("BTC", "ETH"),
            )
            return await registry.refresh()

    markets = asyncio.run(run())

    assert "near-market" in markets
    assert "search-far" not in markets
