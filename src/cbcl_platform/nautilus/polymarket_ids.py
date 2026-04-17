from __future__ import annotations

from cbcl_platform.models import LiveMarketBinding


def polymarket_instrument_id(condition_id: str, token_id: str | int) -> InstrumentId:
    from nautilus_trader.model.identifiers import InstrumentId

    return InstrumentId.from_str(f"{condition_id}-{token_id}.POLYMARKET")


def binding_instrument_ids(bindings: list[LiveMarketBinding] | tuple[LiveMarketBinding, ...]) -> frozenset[str]:
    instrument_ids: set[str] = set()
    for binding in bindings:
        instrument_ids.add(f"{binding.condition_id}-{binding.yes_token_id}.POLYMARKET")
        instrument_ids.add(f"{binding.condition_id}-{binding.no_token_id}.POLYMARKET")
    return frozenset(instrument_ids)
