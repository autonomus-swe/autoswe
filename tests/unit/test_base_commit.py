"""Starting a run from a named commit rather than from the head of a branch.

SWE-bench pins a `base_commit` per instance, and a patch produced against a branch head
does not apply to it. Without this the benchmark could be run and never scored — the
failures would be caused by the wrong starting point and the number would be
unattributable, which is why `evals/swebench.py` refused to produce predictions at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from api.schemas import RunCreate
from contracts import Budget
from core.errors import RepoError
from orchestrator.state import Phase, RunState
from repo.clone import resolve_commit, resolve_sha
from repo.gitcmd import git

pytestmark = pytest.mark.unit

BODY = {"repo_url": "https://github.com/me/project", "goal": "Implement subtract(a, b)."}


# ---- the request --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["abc1234", "a" * 40, "ABC1234", "  abc1234  ", "0123456789abcdef0123456789abcdef01234567"],
)
def test_a_commit_is_accepted_and_normalised(value: str) -> None:
    got = RunCreate(**BODY, base_commit=value).base_commit
    assert got == value.strip().lower()


@pytest.mark.parametrize(
    "value",
    [
        "abc123",  # six is ambiguous in any repository worth running this against
        "a" * 41,
        "main",  # a branch, which `base_branch` already says
        "HEAD",
        "abc123g",  # not hex
        "",
    ],
)
def test_anything_that_is_not_a_commit_is_refused(value: str) -> None:
    """Accepting a ref here would make `base_branch` and `base_commit` two ways to say the
    same thing, which eventually disagree."""
    with pytest.raises(ValidationError):
        RunCreate(**BODY, base_commit=value)


def test_no_commit_is_the_default() -> None:
    assert RunCreate(**BODY).base_commit is None


def test_a_commit_does_not_replace_the_branch() -> None:
    """Both matter, and they are not alternatives. The commit is where the work starts;
    the branch is what the pull request targets. "Start here, merge there" is exactly what
    a benchmark instance asks for."""
    body = RunCreate(**BODY, base_commit="a" * 40, base_branch="develop")
    assert body.base_commit == "a" * 40 and body.base_branch == "develop"


# ---- the resolution -----------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A bare mirror with two commits on `main`, as `ensure_bare_clone` would leave it."""
    import asyncio

    work = tmp_path / "work"
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
        await git("clone", "--bare", "-q", str(work), str(tmp_path / "bare.git"))

    asyncio.run(build())
    return tmp_path / "bare.git"


async def test_a_full_sha_resolves_to_itself(repo: Path) -> None:
    head = await resolve_sha(repo, "main")
    assert await resolve_commit(repo, head) == head


async def test_an_abbreviated_sha_expands(repo: Path) -> None:
    """The caller may have seven characters; everything downstream wants forty, and a
    worktree created at an abbreviation is a worktree nobody can name later."""
    head = await resolve_sha(repo, "main")
    assert await resolve_commit(repo, head[:7]) == head


async def test_an_earlier_commit_resolves_rather_than_the_head(repo: Path) -> None:
    """The whole point: a run must be able to start somewhere other than the tip."""
    head = await resolve_sha(repo, "main")
    parent = (await git("rev-parse", f"{head}^", cwd=repo)).strip()
    assert parent != head
    assert await resolve_commit(repo, parent) == parent


async def test_a_commit_that_is_not_here_says_why(repo: Path) -> None:
    """git's own message — "unknown revision or path not in the working tree" — reads as a
    typo, and the usual cause is not one: the mirror fetches branch heads, so a commit on
    no branch is simply absent. That is a fact about the repository, not the request."""
    with pytest.raises(RepoError, match="fetches branch heads"):
        await resolve_commit(repo, "0" * 40)


async def test_a_branch_name_is_not_silently_accepted_as_a_commit(repo: Path) -> None:
    """`rev-parse main` would succeed and quietly make `base_commit` a second spelling of
    `base_branch`. The schema refuses it first; this is the layer below saying the same.
    """
    resolved = await resolve_commit(repo, "main")
    # git does resolve it — which is exactly why the schema, not this, is the gate.
    assert resolved == await resolve_sha(repo, "main")
    with pytest.raises(ValidationError):
        RunCreate(**BODY, base_commit="main")


# ---- what the run actually starts from ------------------------------------------------------


def run_state(**kw: Any) -> RunState:
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "goal": "Implement subtract(a, b).",
        "repo_url": "https://github.com/me/project",
        "base_branch": "main",
        "work_branch": "agent/x",
        "phase": Phase.SETUP,
        "budget": Budget(),
    }
    return RunState(**{**base, **kw})


async def test_without_a_commit_the_run_starts_at_the_branch_head(repo: Path) -> None:
    from orchestrator.nodes import starting_commit

    head = await resolve_sha(repo, "main")
    assert await starting_commit(repo, run_state()) == head


async def test_with_a_commit_the_run_starts_there_and_not_at_the_head(repo: Path) -> None:
    """The behaviour the whole change exists for. A node that read the branch anyway would
    produce a patch against the wrong base and a benchmark score nobody could attribute."""
    from orchestrator.nodes import starting_commit

    head = await resolve_sha(repo, "main")
    parent = (await git("rev-parse", f"{head}^", cwd=repo)).strip()

    got = await starting_commit(repo, run_state(base_commit=parent))
    assert got == parent
    assert got != head


async def test_an_abbreviated_commit_is_expanded_before_anything_uses_it(repo: Path) -> None:
    from orchestrator.nodes import starting_commit

    head = await resolve_sha(repo, "main")
    got = await starting_commit(repo, run_state(base_commit=head[:7]))
    assert got == head


async def test_the_worktree_can_be_created_at_a_commit(repo: Path, tmp_path: Path) -> None:
    """`git worktree add -b <branch> <path> <commit>` — the run branches from the commit
    rather than from whatever the branch points at by then."""
    from repo import worktree as wt

    head = await resolve_sha(repo, "main")
    parent = (await git("rev-parse", f"{head}^", cwd=repo)).strip()

    tree = await wt.create(repo, tmp_path / "wts", "run-1", parent)
    try:
        assert (await git("rev-parse", "HEAD", cwd=tree.path)).strip() == parent
        assert (tree.path / "a.txt").read_text() == "one\n"  # the earlier commit's content
    finally:
        await wt.remove(tree)
