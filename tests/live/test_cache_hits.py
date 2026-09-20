"""Prompt caching, measured against a provider that actually reports cache reads.

`tests/unit/test_caching.py` checks that the right bytes are assembled and the right blocks
marked. None of that can tell you whether a provider then *read* the cache — that number
exists only on the other side of a real call, in `usage.cache_read_tokens`.

Ollama reports it. `prompt_tokens_details.cached_tokens` comes back on every response and
`usage_from` already parses it, so the whole design from the caching step can be checked on
a laptop with no key and no quota. I had assumed this needed paid credit and it does not;
an embedding needs one forward pass and a cache hit needs a second identical prefix, and
neither is a generation budget.

**The control is the test.** A provider will cache an identical request whatever we do, so
"the second call had cache reads" proves nothing about *our* prefix. What proves it is the
pair: hold the prefix still and the hit rate goes to ~1.0; change one byte near the front
and it collapses. That is the property `gateway/caching` exists to protect, and the only
one worth asserting.
"""

from __future__ import annotations

import os

import pytest

from contracts import Usage
from gateway.openai_compat_provider import OpenAICompatProvider
from gateway.provider import Request

pytestmark = pytest.mark.live

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("CACHE_TEST_MODEL", "qwen2.5:3b")
# Long enough that a miss is unmistakable; short enough to run in seconds on a laptop.
SYSTEM = "You are a terse assistant that answers in one word. " * 300
RUN_BLOCK = "# Repository\n" + "\n".join(f"file_{i}.py defines thing_{i}" for i in range(80))


def _model_ready() -> bool:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
        return any(n.split(":")[0] == MODEL.split(":")[0] for n in names)
    except Exception:
        return False


requires_model = pytest.mark.skipif(
    not _model_ready(), reason=f"{MODEL} not available at {OLLAMA} (`ollama pull {MODEL}`)"
)


def provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(model=MODEL, api_key=None, base_url=f"{OLLAMA}/v1")


async def one_turn(p: OpenAICompatProvider, system: str) -> Usage:
    """One model call with the given system prefix, returning its usage."""
    req = Request(
        role="coder",
        system=system,
        run_block=RUN_BLOCK,
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        max_tokens=5,
    )
    turn = await p._complete(
        messages=[p._system(req), *req.messages],
        tools=[],
        tool_choice=None,
        max_tokens=5,
        model=MODEL,
    )
    return turn.usage


# ---- the criterion ---------------------------------------------------------------------


@requires_model
async def test_cache_reads_appear_from_the_second_turn_onward() -> None:
    """The Phase 5 criterion, against a provider that reports the number.

    Three turns because the criterion says three: the first pays for the prefix, and every
    one after it should be reading rather than writing.
    """
    p = provider()

    usages = [await one_turn(p, SYSTEM) for _ in range(3)]

    first, *rest = usages
    assert all(u.cache_read_tokens > 0 for u in rest), [u.cache_read_tokens for u in usages]
    assert all(u.cache_hit_rate > 0.6 for u in rest), [u.cache_hit_rate for u in rest]
    assert first.cache_hit_rate < 0.5, "the first turn should be writing the cache, not reading it"


@requires_model
async def test_a_prefix_that_moves_loses_the_cache() -> None:
    """The control, and the reason the test above means anything.

    A provider caches an identical request whatever the harness does, so "the second call
    had cache reads" says nothing about our prefix. Changing one line near the front of the
    system block is exactly the silent invalidator `gateway/caching` exists to prevent — a
    date, a run id, a turn counter — and the hit rate has to collapse when it happens.
    """
    p = provider()
    await one_turn(p, SYSTEM)  # warm whatever this provider wants to warm

    steady = await one_turn(p, SYSTEM)
    moved = await one_turn(p, f"Run 7f3a started at 12:04. {SYSTEM}")

    assert steady.cache_hit_rate > 0.6, steady
    assert moved.cache_hit_rate < steady.cache_hit_rate, (
        f"a moved prefix still hit the cache: {moved} vs {steady}"
    )


@requires_model
async def test_the_run_block_is_inside_the_cached_prefix() -> None:
    """The repository context is the larger half of the prefix and the half that would be
    re-sent uncached if it lived in the messages, which is where it used to live."""
    p = provider()
    await one_turn(p, SYSTEM)

    steady = await one_turn(p, SYSTEM)

    # Everything but the volatile user turn came back from cache; the run block is most of
    # what that covers.
    assert steady.cache_read_tokens > len(RUN_BLOCK) // 8, steady
