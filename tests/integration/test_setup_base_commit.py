"""SETUP starts the run at the commit the run asked for.

`tests/unit/test_base_commit.py` checks `starting_commit` in isolation. That is not
enough, and a mutation proved it: replacing the call in `setup_node` with the old
`resolve_sha(bare, state.base_branch)` left the whole suite green. The same shape dropped
`upstream` from the pull-request node earlier in this phase — a one-line choice inside a
node nothing exercises end to end.

So this drives the real node against a real repository and a real Redis lock, and stops it
at the first thing that needs Docker. What SETUP had already decided by then is exactly
what is under test: the commit recorded on the state, and the commit the worktree is on.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from contracts import Budget
from core.settings import load_settings
from orchestrator.deps import Deps
from orchestrator.nodes import RunResources, setup_node
from orchestrator.state import Phase, RunState
from repo.gitcmd import git
from storage.redis import RedisBus

pytestmark = pytest.mark.integration


class NoDocker(Exception):
    """Raised by the stand-in sandbox factory, to stop SETUP where Docker would begin."""


@pytest.fixture
def origin(host_tmp: Path) -> Path:
    """A repository with two commits, so "the head" and "a commit" are different answers."""
    work = host_tmp / "origin"
    work.mkdir()

    async def build() -> None:
        await git("init", "-q", "-b", "main", cwd=work)
        await git("config", "user.email", "t@example.com", cwd=work)
        await git("config", "user.name", "t", cwd=work)
        (work / "a.txt").write_text("one\n")
        await git("add", "-A", cwd=work)
        await git("commit", "-q", "-m", "one", cwd=work)
        (work / "a.txt").write_text("two\n")
        await git("add", "-A", cwd=work)
        await git("commit", "-q", "-m", "two", cwd=work)

    asyncio.run(build())
    return work


async def commits(repo: Path) -> tuple[str, str]:
    """`(head, parent)`."""
    head = (await git("rev-parse", "HEAD", cwd=repo)).strip()
    parent = (await git("rev-parse", "HEAD^", cwd=repo)).strip()
    return head, parent


def deps_for(bus: RedisBus, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Deps:
    monkeypatch.setenv("WORKTREES_DIR", str(tmp / "wts"))
    monkeypatch.setenv("REPOS_DIR", str(tmp / "repos"))

    def factory(*a: Any, **k: Any) -> Any:
        raise NoDocker("SETUP reached the sandbox, which is as far as this test goes")

    return Deps(
        settings=load_settings(env_file=None),
        provider=None,  # type: ignore[arg-type]
        engine=None,
        bus=bus,
        sandbox_factory=factory,
        github=None,
    )


async def run_setup(
    bus: RedisBus, tmp: Path, monkeypatch: pytest.MonkeyPatch, state: RunState
) -> tuple[RunState, RunResources]:
    """Drive SETUP until it needs Docker, then clean up after it."""
    deps = deps_for(bus, tmp, monkeypatch)
    res = RunResources()
    try:
        with pytest.raises(NoDocker):
            await setup_node(state, deps, res)
    finally:
        if res.renewer is not None:
            res.renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await res.renewer
        if res.lock_key is not None:
            await bus.release_lock(res.lock_key, res.lock_owner or "")
    return state, res


def a_run(origin: Path, **kw: Any) -> RunState:
    run_id = uuid4()
    base: dict[str, Any] = {
        "run_id": run_id,
        "goal": "Implement subtract(a, b).",
        "repo_url": str(origin),
        "base_branch": "main",
        "work_branch": f"agent/{run_id}",
        "phase": Phase.SETUP,
        "budget": Budget(),
    }
    return RunState(**{**base, **kw})


async def test_without_a_commit_setup_starts_at_the_branch_head(
    origin: Path, bus: RedisBus, host_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, _parent = await commits(origin)
    state, res = await run_setup(bus, host_tmp, monkeypatch, a_run(origin))

    assert state.base_sha == head
    assert res.worktree is not None
    assert (await git("rev-parse", "HEAD", cwd=res.worktree.path)).strip() == head


async def test_with_a_commit_setup_starts_there(
    origin: Path, bus: RedisBus, host_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour the change exists for, checked where it actually happens.

    Both halves matter and they can disagree: a node could record the right `base_sha` and
    still create the worktree from the branch, which would produce a diff against a base
    the run never had.
    """
    head, parent = await commits(origin)
    state, res = await run_setup(bus, host_tmp, monkeypatch, a_run(origin, base_commit=parent))

    assert state.base_sha == parent != head
    assert res.worktree is not None
    assert (await git("rev-parse", "HEAD", cwd=res.worktree.path)).strip() == parent
    assert (res.worktree.path / "a.txt").read_text() == "one\n"  # the earlier content


async def test_an_abbreviated_commit_is_expanded_on_the_state(
    origin: Path, bus: RedisBus, host_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything downstream keys off `base_sha` — the symbol index, the embeddings, a
    resume. An abbreviation recorded there is a cache key nothing else will match."""
    _head, parent = await commits(origin)
    state, _res = await run_setup(bus, host_tmp, monkeypatch, a_run(origin, base_commit=parent[:7]))
    assert state.base_sha == parent


async def test_a_commit_that_is_not_in_the_mirror_stops_setup(
    origin: Path, bus: RedisBus, host_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rather than proceeding to a worktree at a commit that does not exist and failing a
    step later with a message about worktrees."""
    deps = deps_for(bus, host_tmp, monkeypatch)
    res = RunResources()
    state = a_run(origin, base_commit="0" * 40)
    try:
        with pytest.raises(Exception, match="fetches branch heads"):
            await setup_node(state, deps, res)
    finally:
        if res.renewer is not None:
            res.renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await res.renewer
        if res.lock_key is not None:
            await bus.release_lock(res.lock_key, res.lock_owner or "")
    assert state.base_sha is None
