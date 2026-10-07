"""The ablation arm: make the reading agents take the v1 tree instead of the ranked map.

Phase 5 step 5.10 prescribes an ablation — "run once more with `repomap=off` (Coder and
Planner get the v1 tree only)" — and until now there was no way to perform it. v1 was
reachable only by the symbol index being empty, which is a fault path: an unsupported
language, a repository nothing parsed, an indexing pass that failed. Running the ablation
that way would change the pipeline as well as the map, and the comparison would be between
a ranked map and a broken run rather than between two maps.

So the question the ablation exists to answer — does ranking by centrality and goal
actually beat an indented tree, on a real task — could not be asked. `docs/numbers.md`
says as much, and has to leave the v1-vs-v2 comparison at "they are not substitutes".

What these tests pin is narrow: that the switch reaches the decision, that the v1 arm
returns *before* the index is consulted, and that v2 stays the default. The value of the
ablation is in what a pair of runs shows; the value here is only that the arm is honest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from contracts import Budget
from core.settings import Settings
from orchestrator import nodes
from orchestrator.nodes import RunResources, _repo_map
from orchestrator.state import Phase, RunState
from repo.worktree import Worktree

pytestmark = pytest.mark.unit


# `Settings` requires `database_url`, `redis_url` and `API_KEYS`, and reads `.env` from the
# repository root when they are absent from the environment. Supplying them here rather than
# letting that happen is what makes these tests say the same thing on a developer's machine
# and in CI: without it they passed locally, off a `.env` nobody mentioned, and failed in CI
# with three pydantic validation errors that named settings the tests do not care about.
REQUIRED_BY_SETTINGS: dict[str, Any] = {
    "database_url": "postgresql+asyncpg://u:p@localhost:5432/unused",
    "redis_url": "redis://localhost:6379/0",
    "API_KEYS": "unused-in-this-test",
}


class FakeDeps:
    def __init__(self, version: str) -> None:
        self.settings = Settings(repo_map_version=cast("Any", version), **REQUIRED_BY_SETTINGS)
        self.engine = None


def a_state() -> RunState:
    return RunState(
        run_id=uuid4(),
        goal="Add a __repr__ to the parsed cookie class",
        repo_url="https://github.com/a/b",
        base_branch="main",
        work_branch="agent/x",
        phase=Phase.ANALYZE,
        budget=Budget(),
    )


def resources(tmp_path: Path) -> RunResources:
    return RunResources(
        worktree=Worktree(path=tmp_path, branch="agent/x", bare=tmp_path, run_id="r")
    )


@pytest.fixture
def a_repository(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cookies.py").write_text("class Cookie:\n    pass\n")
    (tmp_path / "README.md").write_text("# demo\n")
    return tmp_path


async def test_the_v1_arm_never_touches_the_symbol_index(
    a_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the arm. A v1 run that still queried and ranked would be measuring the
    rendering while calling it the ranking, and would carry the index's cost besides."""
    opened: list[int] = []

    def watched(_engine: Any) -> _Session:
        opened.append(1)
        return _Session()

    monkeypatch.setattr(nodes, "session", watched)

    rendered = await _repo_map(a_state(), cast("Any", FakeDeps("v1")), resources(a_repository))

    assert opened == [], "the v1 arm opened a database session"
    assert "cookies.py" in rendered, "and it still produced a map"


async def test_v2_is_what_a_deployment_gets_unless_it_says_otherwise(
    a_repository: Path,
) -> None:
    """An ablation switch that silently became the default would be the ablation shipping
    as the product."""
    assert Settings(**REQUIRED_BY_SETTINGS).repo_map_version == "v2"


async def test_the_two_arms_produce_different_maps(
    a_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If they rendered the same bytes the ablation would compare nothing.

    v2 falls back to v1 when the index is empty, which is why this drives v2 through a
    stubbed index with a symbol in it — otherwise both arms would take the same path and
    the test would pass while proving the opposite of what it claims.
    """
    v1 = await _repo_map(a_state(), cast("Any", FakeDeps("v1")), resources(a_repository))

    marker = "# ranked map, not a tree"

    async def symbols_for_sha(*_a: Any, **_k: Any) -> list[Any]:
        return [_symbol_row()]

    monkeypatch.setattr("orchestrator.nodes.repomap.render_symbol_map", lambda *a, **k: marker)
    monkeypatch.setattr(nodes, "session", lambda _e: _Session())
    monkeypatch.setattr("orchestrator.nodes.db.symbols_for_sha", symbols_for_sha)
    monkeypatch.setattr("orchestrator.nodes.graph.build", lambda *a, **k: _Ranks())

    v2 = await _repo_map(a_state(), cast("Any", FakeDeps("v2")), resources(a_repository))

    assert v2 == marker and v1 != marker


# ---- the stubs the v2 path needs ---------------------------------------------------------


class _Ranks:
    def __init__(self) -> None:
        self.rank = {"src/cookies.py": 1.0}


def _symbol_row() -> Any:
    """One indexed symbol, shaped as `db.symbols_for_sha` returns them."""
    return type(
        "Row",
        (),
        {
            "path": "src/cookies.py",
            "kind": "class",
            "name": "Cookie",
            "signature": "class Cookie",
            "start_line": 1,
            "end_line": 2,
            "refs": [],
        },
    )()


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None
