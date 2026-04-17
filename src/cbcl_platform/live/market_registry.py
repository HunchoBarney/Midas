from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from cbcl_platform.config import MarketRegistryConfig
from cbcl_platform.models import ContractInterval, LiveMarketBinding

_DISCOVERY_STAGE_TIMEOUT_S = 4.0
_DISCOVERY_REFRESH_TIMEOUT_S = 8.0


class MarketRegistry:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        config: MarketRegistryConfig,
        allowed_coins: tuple[str, ...],
    ) -> None:
        self._http_client = http_client
        self._config = config
        self._allowed_coins = tuple(str(coin).upper() for coin in allowed_coins)
        self._markets: dict[str, LiveMarketBinding] = {}
        self._token_to_market: dict[str, str] = {}
        self._running = True

    def snapshot(self) -> dict[str, LiveMarketBinding]:
        return dict(self._markets)

    def get(self, market_id: str) -> LiveMarketBinding | None:
        return self._markets.get(market_id)

    def market_for_token(self, token_id: str) -> LiveMarketBinding | None:
        market_id = self._token_to_market.get(token_id)
        return self._markets.get(market_id) if market_id else None

    async def bootstrap(self) -> dict[str, LiveMarketBinding]:
        try:
            await asyncio.wait_for(self.refresh(), timeout=_DISCOVERY_REFRESH_TIMEOUT_S)
        except TimeoutError:
            return self.snapshot()
        return self.snapshot()

    async def run(self, emit: Callable[[dict[str, Any]], None]) -> None:
        while self._running:
            markets = await self.refresh()
            emit({"type": "market_registry", "markets": markets})
            await asyncio.sleep(self._config.active_refresh_interval_s)

    def stop(self) -> None:
        self._running = False

    async def refresh(self) -> dict[str, LiveMarketBinding]:
        now_ns = time.time_ns()
        bindings: list[LiveMarketBinding] = []
        try:
            candidate_markets = await asyncio.wait_for(
                self._fetch_candidate_markets(),
                timeout=_DISCOVERY_REFRESH_TIMEOUT_S,
            )
        except TimeoutError:
            return self.snapshot()
        for market in candidate_markets:
            if not _is_live_market(market):
                continue
            binding = self._parse_market(market)
            if (
                binding is None
                or binding.interval not in {
                    ContractInterval.FIVE_MINUTES,
                    ContractInterval.FIFTEEN_MINUTES,
                }
                or binding.expires_at_ns <= now_ns
            ):
                continue
            bindings.append(binding)
        markets = {
            binding.market_id: binding
            for binding in self._select_active_bindings(bindings, now_ns=now_ns)
        }
        self._markets = markets
        self._token_to_market = {
            binding.yes_token_id: binding.market_id for binding in markets.values()
        } | {binding.no_token_id: binding.market_id for binding in markets.values()}
        return self.snapshot()

    async def resolve_markets(self, market_ids: set[str]) -> list[tuple[LiveMarketBinding, str]]:
        if not market_ids:
            return []
        filters = {
            "condition_ids": ",".join(sorted(market_ids)),
            "limit": len(market_ids),
        }
        results: list[tuple[LiveMarketBinding, str]] = []
        for market in await self._fetch_markets(filters):
            binding = self._parse_market(market)
            condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
            if binding is None or condition_id not in market_ids:
                continue
            winner = _extract_winning_token_id(market, binding)
            if winner:
                results.append((binding, winner))
        return results

    async def _fetch_candidate_markets(self) -> list[dict[str, Any]]:
        now_ns = time.time_ns()
        try:
            exact_markets = await asyncio.wait_for(
                self._fetch_exact_bucket_markets(now_ns=now_ns),
                timeout=_DISCOVERY_STAGE_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001
            exact_markets = []
        if self._has_actionable_markets(exact_markets, now_ns=now_ns):
            return exact_markets
        try:
            fast_markets = await asyncio.wait_for(
                self._fetch_search_markets(),
                timeout=_DISCOVERY_STAGE_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001
            fast_markets = []
        if self._has_actionable_markets(fast_markets, now_ns=now_ns):
            return fast_markets
        filters = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": self._config.max_active_markets,
        }
        try:
            return await asyncio.wait_for(
                self._fetch_markets(filters),
                timeout=_DISCOVERY_STAGE_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001
            return []

    async def _fetch_exact_bucket_markets(self, *, now_ns: int) -> list[dict[str, Any]]:
        tasks = [
            self._fetch_market_by_slug(slug)
            for slug in self._target_market_slugs(now_ns=now_ns)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        markets: dict[str, dict[str, Any]] = {}
        for market in results:
            if isinstance(market, Exception) or not market:
                continue
            slug = str(market.get("slug") or "")
            if slug:
                markets[slug] = market
        return list(markets.values())

    async def _fetch_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        base_url = self._config.gamma_base_url.rstrip("/")
        response = await self._http_client.get(f"{base_url}/markets", params={"slug": slug})
        response.raise_for_status()
        page = _extract_market_list(response.json())
        return page[0] if page else None

    def _target_market_slugs(self, *, now_ns: int) -> list[str]:
        slugs: list[str] = []
        for coin in self._allowed_coins:
            for interval in (
                ContractInterval.FIVE_MINUTES,
                ContractInterval.FIFTEEN_MINUTES,
            ):
                bucket_start_s = _bucket_start_seconds(now_ns=now_ns, interval=interval)
                interval_s = _interval_duration_ns(interval) // 1_000_000_000
                for offset in range(self._config.max_markets_per_slot):
                    start_s = bucket_start_s + offset * interval_s
                    slugs.append(f"{coin.lower()}-updown-{interval.value}-{start_s}")
        return slugs

    def _has_actionable_markets(self, markets: list[dict[str, Any]], *, now_ns: int) -> bool:
        bindings: list[LiveMarketBinding] = []
        for market in markets:
            if not _is_live_market(market):
                continue
            binding = self._parse_market(market)
            if binding is None or binding.expires_at_ns <= now_ns:
                continue
            bindings.append(binding)
        if not bindings:
            return False
        selected = self._select_active_bindings(bindings, now_ns=now_ns)
        if not selected:
            return False
        for binding in selected:
            horizon_ns = _actionable_horizon_ns(
                binding.interval,
                self._config.max_markets_per_slot,
            )
            if binding.expires_at_ns <= now_ns + horizon_ns:
                return True
        return False

    def _select_active_bindings(
        self,
        bindings: list[LiveMarketBinding],
        *,
        now_ns: int,
    ) -> list[LiveMarketBinding]:
        per_slot: dict[tuple[str, ContractInterval], list[LiveMarketBinding]] = {}
        for binding in bindings:
            per_slot.setdefault((binding.coin, binding.interval), []).append(binding)

        selected: list[LiveMarketBinding] = []
        for slot_bindings in per_slot.values():
            slot_bindings.sort(key=lambda binding: binding.expires_at_ns)
            actionable = [
                binding
                for binding in slot_bindings
                if binding.expires_at_ns
                <= now_ns
                + _actionable_horizon_ns(
                    binding.interval,
                    self._config.max_markets_per_slot,
                )
            ]
            chosen = actionable if actionable else slot_bindings
            selected.extend(chosen[: self._config.max_markets_per_slot])
        return selected

    async def _fetch_search_markets(self) -> list[dict[str, Any]]:
        base_url = self._config.gamma_base_url.rstrip("/")
        queries = {
            "BTC": "Bitcoin Up or Down",
            "ETH": "Ethereum Up or Down",
        }
        markets: dict[str, dict[str, Any]] = {}
        base_date = datetime.now(UTC)
        for coin in self._allowed_coins:
            prefix = queries.get(coin)
            if prefix is None:
                continue
            for day_offset in range(3):
                try:
                    day = base_date + timedelta(days=day_offset)
                    query = f"{prefix} {day.strftime('%B')} {day.day} {day.year}"
                    response = await self._http_client.get(
                        f"{base_url}/public-search",
                        params={"q": query, "limit_per_type": 100},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    for market in _extract_search_market_list(payload):
                        slug = str(market.get("slug") or "")
                        if slug:
                            markets[slug] = market
                    await asyncio.sleep(0)
                except Exception:  # noqa: BLE001
                    continue
        return list(markets.values())

    async def _fetch_markets(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        base_url = self._config.gamma_base_url.rstrip("/")
        page_size = max(1, int(filters.get("limit") or self._config.max_active_markets))
        offset = int(filters.get("offset") or 0)
        results: list[dict[str, Any]] = []

        while True:
            params = dict(filters)
            params["limit"] = page_size
            params["offset"] = offset

            response = await self._http_client.get(f"{base_url}/markets", params=params)
            response.raise_for_status()
            payload = response.json()
            page = _extract_market_list(payload)
            results.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        return results

    def _parse_market(self, market: dict[str, Any]) -> LiveMarketBinding | None:
        if not _is_updown_market(market):
            return None
        coin = _extract_coin(market, self._allowed_coins)
        if coin is None:
            return None
        interval = _extract_interval(market)
        if interval is None:
            return None
        token_map = _extract_token_map(market)
        yes_token = token_map.get("YES")
        no_token = token_map.get("NO")
        expires_at_ns = _extract_expiration_ns(market)
        condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
        slug = str(market.get("slug") or market.get("market_slug") or "")
        if not yes_token or not no_token or not condition_id or expires_at_ns <= 0:
            return None
        return LiveMarketBinding(
            market_id=condition_id,
            event_slug=slug,
            coin=coin,
            interval=interval,
            expires_at_ns=expires_at_ns,
            yes_token_id=yes_token,
            no_token_id=no_token,
            condition_id=condition_id,
        )


def _is_updown_market(market: dict[str, Any]) -> bool:
    slug = str(market.get("slug") or market.get("market_slug") or "").lower()
    question = str(market.get("question") or "").lower()
    return "updown" in slug or "up/down" in question or "up or down" in question


def _extract_coin(market: dict[str, Any], allowed_coins: tuple[str, ...]) -> str | None:
    haystack = " ".join(
        [
            str(market.get("slug") or ""),
            str(market.get("question") or ""),
        ],
    ).upper()
    aliases = {
        "BTC": ("BTC", "BITCOIN"),
        "ETH": ("ETH", "ETHEREUM"),
    }
    for coin in allowed_coins:
        terms = aliases.get(coin, (coin,))
        if any(term in haystack for term in terms):
            return coin
    return None


def _extract_interval(market: dict[str, Any]) -> ContractInterval | None:
    haystack = " ".join(
        [
            str(market.get("slug") or ""),
            str(market.get("question") or ""),
        ],
    ).lower()
    if re.search(r"(^|[^0-9])15\s*(m|min|minute)", haystack):
        return ContractInterval.FIFTEEN_MINUTES
    if re.search(r"(^|[^0-9])5\s*(m|min|minute)", haystack):
        return ContractInterval.FIVE_MINUTES
    if _is_updown_market(market) and re.search(r"\b\d{1,2}\s*(am|pm)\b", haystack):
        return ContractInterval.HOURLY
    return None


def _extract_token_map(market: dict[str, Any]) -> dict[str, str]:
    token_ids = market.get("clobTokenIds") or market.get("clob_token_ids") or []
    outcomes = market.get("outcomes") or []
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if not isinstance(token_ids, list):
        token_ids = []
    if not isinstance(outcomes, list):
        outcomes = []
    result: dict[str, str] = {}
    for token_id, outcome in zip(token_ids, outcomes, strict=False):
        norm = _normalize_outcome_label(outcome)
        if norm is not None:
            result[norm] = str(token_id)
    return result


def _extract_expiration_ns(market: dict[str, Any]) -> int:
    value = (
        market.get("endDate")
        or market.get("end_date")
        or market.get("endDateIso")
        or market.get("end_date_iso")
    )
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1e9)
    except ValueError:
        return 0


def _extract_winning_token_id(market: dict[str, Any], binding: LiveMarketBinding) -> str | None:
    tokens = market.get("tokens")
    if isinstance(tokens, list):
        for token in tokens:
            if not isinstance(token, dict):
                continue
            if token.get("winner") is True:
                token_id = str(token.get("token_id") or token.get("tokenId") or "")
                if token_id:
                    return token_id
    outcome_prices = market.get("outcomePrices") or market.get("outcome_prices")
    outcomes = market.get("outcomes") or []
    token_ids = market.get("clobTokenIds") or market.get("clob_token_ids") or []
    if isinstance(outcome_prices, str):
        outcome_prices = json.loads(outcome_prices)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if (
        isinstance(outcome_prices, list)
        and isinstance(outcomes, list)
        and isinstance(token_ids, list)
    ):
        for token_id, outcome, price in zip(token_ids, outcomes, outcome_prices, strict=False):
            try:
                if float(price) >= 0.999:
                    norm = _normalize_outcome_label(outcome)
                    if norm == "YES":
                        return binding.yes_token_id
                    if norm == "NO":
                        return binding.no_token_id
                    return str(token_id)
            except (TypeError, ValueError):
                continue
    return None


def _normalize_outcome_label(outcome: Any) -> str | None:
    normalized = str(outcome).strip().upper()
    if normalized in {"YES", "UP"}:
        return "YES"
    if normalized in {"NO", "DOWN"}:
        return "NO"
    return None


def _extract_market_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _extract_search_market_list(payload: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return results
    markets = payload.get("markets")
    if isinstance(markets, list):
        results.extend(item for item in markets if isinstance(item, dict))
    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            nested_markets = event.get("markets")
            if isinstance(nested_markets, list):
                results.extend(item for item in nested_markets if isinstance(item, dict))
    return results


def _is_live_market(market: dict[str, Any]) -> bool:
    active = market.get("active")
    closed = market.get("closed")
    archived = market.get("archived")
    return bool(active is True and closed is False and archived is False)


def _interval_duration_ns(interval: ContractInterval) -> int:
    if interval == ContractInterval.FIVE_MINUTES:
        return 5 * 60 * 1_000_000_000
    if interval == ContractInterval.FIFTEEN_MINUTES:
        return 15 * 60 * 1_000_000_000
    if interval == ContractInterval.HOURLY:
        return 60 * 60 * 1_000_000_000
    raise ValueError(f"Unsupported interval {interval}")


def _bucket_start_seconds(*, now_ns: int, interval: ContractInterval) -> int:
    interval_s = _interval_duration_ns(interval) // 1_000_000_000
    now_s = now_ns // 1_000_000_000
    return (now_s // interval_s) * interval_s


def _actionable_horizon_ns(interval: ContractInterval, max_markets_per_slot: int) -> int:
    # Current and immediate next expiry bucket for the slot, plus a small buffer.
    return _interval_duration_ns(interval) * max(1, max_markets_per_slot) + 60 * 1_000_000_000
