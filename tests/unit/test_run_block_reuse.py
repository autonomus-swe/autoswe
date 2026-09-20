"""A resumed run reuses its repository context instead of rebuilding a different one.

`tests/integration/test_run_block_cache.py` checks that Redis stores and returns the block.
That is the easy half, and on its own it is the `Route.tier` mistake again — a value
carried carefully to a consumer that never reads it. These tests are about the consumer.

The property worth proving is narrow and specific: when a resumed run asks for its run
block, it gets the bytes the *first* process produced, even though re-rendering would
produce different ones. The map is goal-ranked and `_ensure_run_block` declines to promise
stable bytes, so "it would render the same thing anyway" is not available as an argument —
which is exactly why the fake below returns a different map every time it is called.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from contracts import Budget
from orchestrator import nodes
from orchestrator.nodes import RunResources, _ensure_run_block
from orchestrator.state import Phase, RunState

pytestmark = pytest.mark.unit


class FakeBus:
    """Redis, reduced to the two calls this path makes."""

    def __init__(self, stored: str | None = None) -> None:
        self.store: dict[str, str] = {}
        self.writes = 0
        self.seeded = stored

    async def get_run_block(self, run_id: Any) -> str | None:
        if self.seeded is not None and str(run_id) not in self.store:
            return self.seeded
        return self.store.get(str(run_id))

    async def set_run_block(self, run_id: Any, block: str, ttl_s: int = 0) -> None:
        self.store[str(run_id)] = block
        self.writes += 1


class FakeDeps:
    def __init__(self, bus: FakeBus) -> None:
        self.bus = bus
        self.engine = None
        self.settings = None


async def ensure(state: RunState, bus: FakeBus, res: RunResources) -> str:
    """One cast, here, rather than an ignore on every call site."""
    return await _ensure_run_block(state, cast("Any", FakeDeps(bus)), res)


def a_state() -> RunState:
    return RunState(
        run_id=uuid4(),
        goal="Add subtract",
        repo_url="https://github.com/a/b",
        base_branch="main",
        work_branch="agent/x",
        phase=Phase.ANALYZE,
        budget=Budget(),
    )


@pytest.fixture
def unstable_map(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A repo map that renders differently every time, which is the honest case.

    Ranking is against the goal and over a graph rebuilt per process; the production
    docstring says as much. A fake that returned a constant would let a broken
    implementation pass by re-rendering.
    """
    calls = [0]

    async def _repo_map(*_a: Any, **_k: Any) -> str:
        calls[0] += 1
        return f"# Repository (render {calls[0]})"

    monkeypatch.setattr(nodes, "_repo_map", _repo_map)
    return calls


async def test_a_fresh_run_builds_its_block_and_stores_it(unstable_map: list[int]) -> None:
    bus = FakeBus()
    state, res = a_state(), RunResources()

    block = await ensure(state, bus, res)

    assert "render 1" in block
    assert bus.store[str(state.run_id)] == block, "stored exactly what the agents will see"


async def test_a_resumed_run_gets_the_first_processs_bytes_not_a_fresh_render(
    unstable_map: list[int],
) -> None:
    """The whole point.

    Two `RunResources` stand in for two processes — a resume starts with an empty one and
    never re-enters ANALYZE. Without the cache the second process renders map 2, the prefix
    moves, and every agent in the resumed run pays a cache write it already paid.
    """
    bus = FakeBus()
    state = a_state()

    first = await ensure(state, bus, RunResources())
    resumed = await ensure(state, bus, RunResources())

    assert resumed == first, "the resumed run rebuilt a different prefix"
    assert "render 2" not in resumed
    assert unstable_map[0] == 1, "the map was rendered twice; the cache was not consulted"


async def test_within_one_process_the_block_is_not_fetched_twice(
    unstable_map: list[int],
) -> None:
    """`RunResources` still short-circuits. Every step calls this, and a Redis round trip
    per step for a value already in memory is a cost with nothing bought."""
    bus = FakeBus()
    state, res = a_state(), RunResources()

    await ensure(state, bus, res)
    again = await ensure(state, bus, res)

    assert again == res.run_block
    assert bus.writes == 1, "wrote twice for one process"


async def test_an_empty_block_is_still_reused(unstable_map: list[int]) -> None:
    """A repository nothing could be ranked in yields an empty block. Treating that as a
    miss would send every resume back through a full repository walk to learn the same
    nothing — the `None`-versus-`""` distinction, from the consumer's side."""
    bus = FakeBus(stored="")
    state = a_state()

    block = await ensure(state, bus, RunResources())

    assert block == ""
    assert unstable_map[0] == 0, "an empty cached block still triggered a render"
