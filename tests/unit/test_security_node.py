"""The security phase as the orchestrator runs it.

Two rows, and both are about a scan that could not happen rather than one that found
nothing — because those must not look alike to anything downstream.

The push refusal that used to live here moved to `test_pr_node.py` when the gate started
re-scanning immediately before the push instead of reading the phase's result back. That
is where the refusal belongs: it is a property of pushing, not of scanning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from contracts import Budget, SecurityFinding, Task, TaskGraph, TaskSpec
from orchestrator.nodes import RunResources, security_node
from orchestrator.state import Phase, RunState
from repo.worktree import Worktree

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
        self.artifacts: list[tuple[str, Any]] = []


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

    async def save_artifact(s: Any, run_id: Any, kind: str, path: Any, content: Any) -> None:
        rec.artifacts.append((kind, content))

    monkeypatch.setattr(nodes, "push_branch", push_branch)
    monkeypatch.setattr(nodes, "open_pr", open_pr)
    monkeypatch.setattr(nodes, "_emit", emit)
    monkeypatch.setattr(nodes, "git", gitcmd)
    monkeypatch.setattr(nodes, "session", lambda _engine: NullSession())
    monkeypatch.setattr("storage.repo.finish_run", noop)
    monkeypatch.setattr("storage.repo.upsert_tasks", noop)
    monkeypatch.setattr("storage.repo.save_artifact", save_artifact)
    return rec


def resources(tmp_path: Path) -> RunResources:
    return RunResources(
        worktree=Worktree(path=tmp_path, branch="agent/x", bare=tmp_path, run_id="r")
    )


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
    # Written as an artifact, not only onto the state. Otherwise
    # `GET /runs/{id}/artifacts/security` 404s for a run that changed nothing, and a caller
    # cannot tell "scanned, nothing found" from "never scanned".
    (kind, content) = sent.artifacts[0]
    assert kind == "security" and content["report"]["findings"] == []


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
    assert [k for k, _ in sent.artifacts] == ["security"], "still on the record"
