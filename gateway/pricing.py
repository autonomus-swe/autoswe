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

# Endpoints whose models genuinely cost nothing, so a zero is a measurement and not a
# missing price. Local inference is the honest zero; ":free" is what OpenRouter calls its
# no-charge tier.
FREE_PREFIXES = ("ollama/", "local/")
FREE_SUFFIXES = (":free",)
# OpenRouter's auto-router over its no-charge tier. Named, not suffixed.
FREE_NAMES = ("openrouter/free",)
# A local endpoint bills nothing whatever the model is called, and a bare `qwen2.5:7b`
# says nothing about where it runs — so the endpoint decides, not the name.
LOCAL_HOSTS = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",  # noqa: S104 — a hostname to match in a URL, not an address to bind
    "::1",
    "host.docker.internal",
)

_warned: set[str] = set()


def is_local(base_url: str | None) -> bool:
    return bool(base_url) and any(h in str(base_url) for h in LOCAL_HOSTS)


def is_free(model: str, base_url: str | None = None) -> bool:
    return (
        model in FREE_NAMES
        or model.endswith(FREE_SUFFIXES)
        or model.startswith(FREE_PREFIXES)
        or is_local(base_url)
    )


def price_for(model: str, base_url: str | None = None) -> Price | None:
    """The price for a model, or None when we cannot know it.

    None and ``Price(0, 0)`` mean different things and callers depend on the difference:
    a free model costs nothing, an unpriced one costs an unknown amount, and a dollar
    budget can only be enforced against the first.
    """
    if model in PRICES:
        return PRICES[model]
    # Vendors ship dated ids — claude-haiku-4-5-20251001 is priced as claude-haiku-4-5.
    # Longest first, so sonnet-5 never matches a hypothetical sonnet-5-mini's entry.
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(name):
            return PRICES[name]
    if is_free(model, base_url):
        return Price(0.0, 0.0)
    if model not in _warned:
        _warned.add(model)
        log.warning("unknown_model_pricing", model=model)
    return None


def priced(model: str, base_url: str | None = None) -> bool:
    """Can spend be measured at all? A dollar budget needs this to be true."""
    return price_for(model, base_url) is not None


def cost(model: str, usage: Usage, base_url: str | None = None) -> float:
    p = price_for(model, base_url)
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
