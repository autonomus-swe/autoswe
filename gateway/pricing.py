"""$ per million tokens. One dict, verified against the vendors' pricing pages on
2026-09-07. Unknown models cost 0 and are logged once; OpenRouter reports exact cost per
call, which the provider prefers over this table when present."""

from __future__ import annotations

from dataclasses import dataclass

from contracts import Usage
from observability.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write_multiplier: float = 1.25
    cache_read_multiplier: float = 0.1


PRICES: dict[str, Price] = {
    "claude-opus-5": Price(5.0, 25.0),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
}

_warned: set[str] = set()


def price_for(model: str) -> Price | None:
    if model in PRICES:
        return PRICES[model]
    if model.endswith(":free"):
        return Price(0.0, 0.0)
    if model not in _warned:
        _warned.add(model)
        log.warning("unknown_model_pricing", model=model)
    return None


def cost(model: str, usage: Usage) -> float:
    p = price_for(model)
    if p is None:
        return 0.0
    per_tok = 1 / 1_000_000
    return round(
        usage.input_tokens * p.input * per_tok
        + usage.output_tokens * p.output * per_tok
        + usage.cache_write_tokens * p.input * p.cache_write_multiplier * per_tok
        + usage.cache_read_tokens * p.input * p.cache_read_multiplier * per_tok,
        6,
    )
