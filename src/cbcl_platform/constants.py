from __future__ import annotations

from math import inf

CRYPTO_MARKET_FEE_TIERS = {
    0.01: 0.0000,
    0.05: 0.0006,
    0.10: 0.0020,
    0.15: 0.0041,
    0.20: 0.0064,
    0.25: 0.0088,
    0.30: 0.0110,
    0.35: 0.0129,
    0.40: 0.0144,
    0.45: 0.0153,
    0.50: 0.0156,
    0.55: 0.0153,
    0.60: 0.0144,
    0.65: 0.0129,
    0.70: 0.0110,
    0.75: 0.0088,
    0.80: 0.0064,
    0.85: 0.0041,
    0.90: 0.0020,
    0.95: 0.0006,
    0.99: 0.0000,
    1.00: 0.0000,
}

GAS_COST_PER_ORDER_USD = 0.0
POLYMARKET_MIN_SHARES_PER_ORDER = 5.0
POLYMARKET_PRICE_TICK = 0.01
POLYMARKET_MIN_PRICE = 0.01
POLYMARKET_MAX_PRICE = 0.99


def clamp_binary_price(value: float) -> float:
    value = min(POLYMARKET_MAX_PRICE, max(POLYMARKET_MIN_PRICE, float(value)))
    return round(value, 2)


def fee_rate_for_price(price: float) -> float:
    value = max(0.0, min(1.0, float(price)))
    threshold = min(
        (candidate for candidate in CRYPTO_MARKET_FEE_TIERS if value <= candidate),
        default=inf,
    )
    return float(CRYPTO_MARKET_FEE_TIERS.get(threshold, 0.0))
