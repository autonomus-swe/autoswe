"""The security phase as the orchestrator runs it, and the one gate that is not a gate.

Every other gate in this project blocks the *run*: findings go on `known_issues`, the fix
budget gets spent, and a draft pull request that names what is wrong is a better outcome
than none. A committed secret is the exception, because it is the only finding that gets
worse by being transmitted — a force push does not un-index a branch a forge has already
seen, and a pull request body carries it into notification email. So that one refuses to
push at all, and these tests assert the refusal happens *before* anything is sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from contracts import Budget, SecurityFinding, SecurityReport, Task, TaskGraph, TaskSpec
from core.errors import RepoError
from orchestrator.nodes import RunResources, pr_node, security_node
from orchestrator.state import Phase, RunState
from repo.worktree import Worktree
from tests.fakes import planted_secret

pytestmark = pytest.mark.unit


def spec(task_id: str = "t1") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title="t",
        description="d",
        depends_on=[],
        files=["src/a.py"],
        acceptance_criteria=["works"],
        test_selector="tests/",
    )


def leak(file: str = "config.py", line: int = 7) -> SecurityFinding:
    return SecurityFinding(
        tool="gitleaks",
        rule="generic-api-key",
        file=file,
        line=line,
        severity="critical",
        message=f"a key was committed in {file} (value withheld)",
        verified_by_llm=True,
        false_positive=False,
        rationale="confirmed against the commit",
        in_diff=True,
    )


def state(**kw: Any) -> RunState:
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "goal": "g",
        "repo_url": "https://github.com/a/b",
        "base_branch": "main",
        "work_branch": "agent/x",
        "phase": Phase.SECURITY,
        "tasks": TaskGraph(tasks=[Task(spec=spec(), status="done")]),
        "budget": Budget(max_fix_rounds=2),
    }
    return RunState(**{**base, **kw})


class FakeDeps:
    def __init__(self) -> None:
        self.engine = None
        self.bus = None
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.provider = object()
        self.settings = None
        self.github = object()

    def git_token(self) -> str | None:
        return None


class Recorder:
    """What left the machine. Empty is the assertion in the refusal tests."""

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.prs: list[str] = []


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Stub everything with an outside effect, and record whether it happened."""
    import orchestrator.nodes as nodes

    rec = Recorder()

    async def push_branch(worktree: Path, branch: str, token: str | None = None) -> None:
        rec.pushed.append(branch)

    async def open_pr(*a: Any, **kw: Any) -> str:
        rec.prs.append(kw.get("head", "?"))
        return "https://github.com/a/b/pull/1"

    async def emit(deps: Any, run_id: Any, type: str, payload: dict[str, Any]) -> None:
        deps.events.append((type, payload))

    async def gitcmd(*a: Any, **kw: Any) -> str:
        return " src/a.py | 2 +-"

    class NullSession:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def noop(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(nodes, "push_branch", push_branch)
    monkeypatch.setattr(nodes, "open_pr", open_pr)
    monkeypatch.setattr(nodes, "_emit", emit)
    monkeypatch.setattr(nodes, "git", gitcmd)
    monkeypatch.setattr(nodes, "session", lambda _engine: NullSession())
    monkeypatch.setattr("storage.repo.finish_run", noop)
    monkeypatch.setattr("storage.repo.upsert_tasks", noop)
    return rec


def resources(tmp_path: Path) -> RunResources:
    return RunResources(
        worktree=Worktree(path=tmp_path, branch="agent/x", bare=tmp_path, run_id="r")
    )


# ---- the push refusal -------------------------------------------------------------------


async def test_a_committed_secret_stops_the_push_before_it_happens(
    tmp_path: Path, sent: Recorder
) -> None:
    """The order is the point. A check after the push would be a check of a published key."""
    s = state(
        phase=Phase.PR,
        security=SecurityReport(findings=[leak()], critical=True, checklist={}),
    )

    with pytest.raises(RepoError, match="refusing to push"):
        await pr_node(s, cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.pushed == [], "nothing was pushed"
    assert sent.prs == [], "and no pull request was opened"


async def test_the_refusal_names_the_file_and_never_the_value(
    tmp_path: Path, sent: Recorder
) -> None:
    """The error goes into a log, a step record and an API response. All three are places
    the value must not reach."""
    secret = planted_secret("refusal")
    found = leak().model_copy(update={"message": f"a key was committed (value withheld) {secret}"})
    s = state(
        phase=Phase.PR, security=SecurityReport(findings=[found], critical=True, checklist={})
    )

    with pytest.raises(RepoError) as caught:
        await pr_node(s, cast("Any", FakeDeps()), resources(tmp_path))

    assert "config.py:7" in str(caught.value), "a human has to know what to rotate"
    assert secret not in str(caught.value)
    assert "rotate" in str(caught.value)


async def test_a_run_with_no_secret_pushes_and_opens_its_pull_request(
    tmp_path: Path, sent: Recorder
) -> None:
    """The control. Without this the refusal test would pass on a node that never pushes."""
    s = state(
        phase=Phase.PR,
        security=SecurityReport(
            findings=[
                SecurityFinding(
                    tool="bandit",
                    rule="B324",
                    file="src/a.py",
                    line=6,
                    severity="high",
                    message="weak hash",
                    verified_by_llm=True,
                    false_positive=False,
                    rationale="reachable",
                    in_diff=True,
                )
            ],
            critical=True,
            checklist={},
        ),
    )

    out = await pr_node(s, cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.pushed == ["agent/x"], "a bandit finding blocks the run, not the transmission"
    assert out.pr_url == "https://github.com/a/b/pull/1"


async def test_a_run_that_never_scanned_is_not_blocked_from_pushing(
    tmp_path: Path, sent: Recorder
) -> None:
    """`state.security` is None when the run changed nothing or had no sandbox. Refusing
    then would strand every such run with no way to produce an outcome."""
    s = state(phase=Phase.PR, security=None)

    await pr_node(s, cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.pushed == ["agent/x"]


# ---- the node itself ---------------------------------------------------------------------


async def test_a_run_that_changed_nothing_records_an_empty_report(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded rather than skipped, so the artifact set is the same shape for every run —
    and so "was this scanned" never has to be inferred from an absence."""
    import orchestrator.nodes as nodes

    async def no_diff(*a: Any, **kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(nodes, "_review_diff", no_diff)
    s = state()

    out = await security_node(s, cast("Any", FakeDeps()), resources(tmp_path))

    assert out.security is not None
    assert out.security.findings == [] and out.security.critical is False


async def test_no_sandbox_is_a_recorded_skip_rather_than_a_crash(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two of the four scanners run inside the sandbox, so there is nothing to run without
    one. A resumed run that lost its sandbox should still reach a pull request."""
    import orchestrator.nodes as nodes
    from repo import diff as d

    async def one_file(*a: Any, **kw: Any) -> list[d.FileDiff]:
        return [d.FileDiff(path="src/a.py", added=1, removed=0, text="+x = 1\n")]

    monkeypatch.setattr(nodes, "_review_diff", one_file)
    res = resources(tmp_path)
    assert res.sandbox is None, "the premise"

    out = await security_node(state(), cast("Any", FakeDeps()), res)

    assert out.security is not None and out.security.critical is False
