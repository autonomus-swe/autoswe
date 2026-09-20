"""The repository context survives a resume, byte for byte.

`_ensure_run_block` freezes the repo map, facts and profile into one block that sits in
every agent's cached prefix for the life of a run. It was held only in `RunResources`,
which is process memory — and the one case that matters is precisely the one that loses
it. A resumed run starts in a fresh process with an empty `RunResources`, so it rebuilds
the block from scratch.

Rebuilding is not free, and the cost is not the render. The map is *goal-ranked*, and
`_ensure_run_block`'s own docstring declines to promise the same bytes twice. A single
differing byte moves the prefix, so every agent in the resumed run pays a full cache write
for context it had already paid to cache once — which is the opposite of what a resume is
for.

The Phase 5 architecture slice says "Redis cached" for exactly this reason. These tests are
about the property that makes it worth doing: not that a value round-trips, but that the
bytes come back *identical*, and that a miss degrades to a re-render rather than to an
error.
"""

from __future__ import annotations

import uuid

import pytest

from storage.redis import RedisBus, run_block_key

pytestmark = pytest.mark.integration


async def test_a_block_comes_back_byte_for_byte(bus: RedisBus) -> None:
    """Identity, not equality-ish. The prefix is matched as bytes by the provider, so a
    block that survives with different whitespace is a block that did not survive."""
    run_id = uuid.uuid4()
    block = (
        "```repository context\n# Repository\nsrc/auth.py\n  def login(user, password)\n"
        "  def logout(session_id)\n\nfacts: python, uv, pytest\n```"
    )

    await bus.set_run_block(run_id, block)

    assert await bus.get_run_block(run_id) == block


async def test_a_miss_is_none_so_the_caller_re_renders(bus: RedisBus) -> None:
    """A cold or evicted Redis has to be slower, never wrong."""
    assert await bus.get_run_block(uuid.uuid4()) is None


async def test_an_empty_block_is_a_hit_and_not_a_miss(bus: RedisBus) -> None:
    """The distinction the `None`-vs-`""` check exists for.

    A repository nothing could be ranked in has a run block, and it is empty. Treating
    that as a miss would send every resume back to re-derive the same nothing, which costs
    a full repository walk to learn it.
    """
    run_id = uuid.uuid4()

    await bus.set_run_block(run_id, "")
    result = await bus.get_run_block(run_id)

    assert result == "", "an empty block round-trips"
    assert result is not None, "and is distinguishable from a miss"


async def test_two_runs_on_the_same_repository_do_not_share_a_block(bus: RedisBus) -> None:
    """Keyed on the run rather than the commit, deliberately: the map is ranked against
    the run's goal, so two runs on the same SHA have legitimately different context."""
    first, second = uuid.uuid4(), uuid.uuid4()

    await bus.set_run_block(first, "ranked for goal A")
    await bus.set_run_block(second, "ranked for goal B")

    assert await bus.get_run_block(first) == "ranked for goal A"
    assert await bus.get_run_block(second) == "ranked for goal B"


async def test_the_block_is_given_a_ttl_so_it_cannot_outlive_its_run(bus: RedisBus) -> None:
    """Without an expiry every run ever started would keep its context in Redis forever."""
    run_id = uuid.uuid4()

    await bus.set_run_block(run_id, "context", ttl_s=1234)
    ttl = await bus.r.ttl(run_block_key(run_id))

    assert 0 < ttl <= 1234
