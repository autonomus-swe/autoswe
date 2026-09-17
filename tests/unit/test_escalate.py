"""Escalation: every way out of a stuck run, and the guarantee that there are no others.

This node exists so a failing loop stops. Each row either makes a *different* attempt
possible or ends the run — never the same attempt again, which is what the attempt cap
already ruled out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from contracts import (
    Budget,
    DebugHypothesis,
    Task,
    TaskGraph,
    TaskGraphSpec,
    TaskSpec,
    TestReport,
    Usage,
)
from orchestrator.nodes import RunResources, escalate_node
from orchestrator.state import Phase, RunState
from orchestrator.transition import MAX_DEBUG_ATTEMPTS
from repo.gitcmd import git
from repo.worktree import Worktree

pytestmark = pytest.mark.unit


def spec(task_id: str, title: str = "do the thing") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=title,
        description="d",
        depends_on=[],
        files=["a.py"],
        acceptance_criteria=["it works"],
        test_selector="tests/",
    )


def report() -> TestReport:
    return TestReport(
        passed=False,
        total=1,
        failed=1,
        errors=0,
        skipped=0,
        failures=[],
        duration_s=0.1,
        command="pytest -q",
        truncated_output="",
        signature="sig-a",
    )


def state(**kw: Any) -> RunState:
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "goal": "g",
        "repo_url": "https://github.com/a/b",
        "base_branch": "main",
        "work_branch": "agent/x",
        "last_test_report": report(),
    }
    return RunState(**{**base, **kw})


class FakeDeps:
    """Just enough Deps for escalate_node: it emits, reads steps, and may replan."""

    def __init__(self, *, replan: TaskGraphSpec | None = None, steps: list[Any] | None = None):
        self.engine = None
        self.bus = None
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._replan = replan
        self._steps = steps or []
        self.provider = object()
        self.settings = None
        # A real Deps always has this. None means "no GitHub client", which is what stops
        # `_fail` from trying to open a draft pull request in these tests — relying on the
        # worktree check short-circuiting first would make the fake a trap.
        self.github = None


@pytest.fixture(autouse=True)
def _stub_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """escalate_node's collaborators, stubbed. The decisions are what is under test."""
    import orchestrator.nodes as nodes

    async def emit(deps: Any, run_id: Any, type: str, payload: dict[str, Any]) -> None:
        deps.events.append((type, payload))

    async def previous(deps: Any, run_id: Any, task_id: str) -> list[DebugHypothesis]:
        return [
            DebugHypothesis(
                failure_class="assertion", root_cause="a theory", plan="a plan", confidence=0.5
            )
        ]

    async def begin(st: Any, deps: Any, res: Any, agent: str, phase: Any) -> tuple[Any, Any, Any]:
        class Hooks:
            usage = Usage()

        return uuid4(), Hooks(), None

    async def end(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(nodes, "_emit", emit)
    monkeypatch.setattr(nodes, "_previous_hypotheses", previous)
    monkeypatch.setattr(nodes, "_begin", begin)
    monkeypatch.setattr(nodes, "_end", end)

    class FakeDecomposer:
        async def replan(
            self, provider: Any, goal: str, task: Any, rep: Any, hyps: Any, hooks: Any = None
        ) -> Any:
            return getattr(provider, "_replan_result", None)

    monkeypatch.setattr(nodes, "DecomposerAgent", FakeDecomposer)

    async def upsert(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr("storage.repo.upsert_tasks", upsert)

    class NullSession:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(nodes, "session", lambda _engine: NullSession())


def deps_with(replan: TaskGraphSpec | None = None) -> Any:
    d = FakeDeps()
    d.provider = type("P", (), {"_replan_result": replan})()
    return d


# ---- budgets: not a problem that trying differently solves -------------------------


@pytest.mark.parametrize("reason", ["budget_usd", "budget_wall_clock", "budget_tokens"])
async def test_a_budget_ends_the_run(reason: str) -> None:
    s = state(phase=Phase.ESCALATE, escalation_reason=reason, budget=Budget())
    out = await escalate_node(s, deps_with(), RunResources())
    assert out.resume_phase is Phase.FAILED
    assert out.error and reason in out.error


# ---- a Coder that produced nothing -------------------------------------------------


async def test_a_coder_with_no_result_gets_another_attempt() -> None:
    tasks = TaskGraph(tasks=[Task(spec=spec("t1"), status="in_progress")])
    s = state(
        phase=Phase.ESCALATE,
        escalation_reason="coder_no_result",
        tasks=tasks,
        current_task_id="t1",
    )
    out = await escalate_node(s, deps_with(), RunResources())
    assert out.resume_phase is Phase.CODE
    assert out.attempts["t1"] == 1
    assert tasks.by_id("t1").status == "pending", "it must be runnable again"


async def test_a_coder_that_keeps_producing_nothing_stops_being_retried() -> None:
    tasks = TaskGraph(tasks=[Task(spec=spec("t1"))])
    s = state(
        phase=Phase.ESCALATE,
        escalation_reason="coder_no_result",
        tasks=tasks,
        current_task_id="t1",
        attempts={"t1": MAX_DEBUG_ATTEMPTS - 1},
        unattended=True,
    )
    out = await escalate_node(s, deps_with(), RunResources())
    # the attempt cap is reached, so this falls through to the exhausted-task rows
    assert out.resume_phase is not Phase.CODE


# ---- an exhausted task: rewind and re-cut, once ------------------------------------


async def test_the_first_exhaustion_replans_into_smaller_tasks() -> None:
    tasks = TaskGraph(tasks=[Task(spec=spec("t1")), Task(spec=spec("t2"))])
    tasks.tasks[1].spec.depends_on = ["t1"]
    s = state(
        phase=Phase.ESCALATE,
        escalation_reason="debug_attempts_exhausted",
        tasks=tasks,
        current_task_id="t1",
        attempts={"t1": MAX_DEBUG_ATTEMPTS},
        previous_failure_signature="sig-a",
        strategy="alternative",
    )
    replan = TaskGraphSpec(tasks=[spec("t1.1", "half one"), spec("t1.2", "half two")])
    out = await escalate_node(s, deps_with(replan), RunResources())

    assert out.resume_phase is Phase.CODE
    assert out.tasks is not None
    assert [t.id for t in out.tasks.tasks] == ["t1.1", "t1.2", "t2"]
    assert all(t.replanned for t in out.tasks.tasks[:2])
    # whatever depended on the old task now depends on all of its parts
    assert out.tasks.by_id("t2").spec.depends_on == ["t1.1", "t1.2"]
    # fresh attempts, and the debug memory is cleared: this is different work now
    assert out.attempts == {"t1.1": 0, "t1.2": 0}
    assert out.previous_failure_signature is None and out.strategy is None


async def test_a_replan_that_comes_back_empty_falls_through_to_the_human() -> None:
    tasks = TaskGraph(tasks=[Task(spec=spec("t1"))])
    s = state(
        phase=Phase.ESCALATE,
        escalation_reason="debug_attempts_exhausted",
        tasks=tasks,
        current_task_id="t1",
        attempts={"t1": MAX_DEBUG_ATTEMPTS},
    )
    out = await escalate_node(s, deps_with(None), RunResources())
    assert out.resume_phase is Phase.AWAITING_INPUT


async def test_a_task_already_replanned_asks_a_human() -> None:
    tasks = TaskGraph(tasks=[Task(spec=spec("t1"), replanned=True)])
    s = state(
        phase=Phase.ESCALATE,
        escalation_reason="debug_attempts_exhausted",
        tasks=tasks,
        current_task_id="t1",
        attempts={"t1": MAX_DEBUG_ATTEMPTS},
    )
    out = await escalate_node(s, deps_with(), RunResources())
    assert out.resume_phase is Phase.AWAITING_INPUT
    assert out.error is None, "asking is not failing"


async def test_an_unattended_run_has_nobody_to_ask_so_it_fails() -> None:
    tasks = TaskGraph(tasks=[Task(spec=spec("t1"), replanned=True)])
    s = state(
        phase=Phase.ESCALATE,
        escalation_reason="debug_attempts_exhausted",
        tasks=tasks,
        current_task_id="t1",
        attempts={"t1": MAX_DEBUG_ATTEMPTS},
        unattended=True,
    )
    out = await escalate_node(s, deps_with(), RunResources())
    assert out.resume_phase is Phase.FAILED
    assert out.error and "after a replan" in out.error


async def test_an_unrecognised_reason_fails_rather_than_guessing() -> None:
    s = state(phase=Phase.ESCALATE, escalation_reason="something_new")
    out = await escalate_node(s, deps_with(), RunResources())
    assert out.resume_phase is Phase.FAILED
    assert out.error and "no way forward" in out.error


async def test_escalating_is_always_announced() -> None:
    s = state(phase=Phase.ESCALATE, escalation_reason="budget_usd")
    d = deps_with()
    await escalate_node(s, d, RunResources())
    kinds = [t for t, _ in d.events]
    assert "escalated" in kinds


# ---- the rewind, against a real repository -----------------------------------------


async def test_the_rewind_discards_the_failed_attempts_edits(tmp_path: Path) -> None:
    """Three attempts leave three half-fixes; a replan must not plan against those."""
    from orchestrator.nodes import _rewind_task

    repo = tmp_path / "wt"
    repo.mkdir()
    await git("init", "-q", "-b", "main", cwd=repo)
    (repo / "ops.py").write_text("def f():\n    return 1\n")
    await git("add", "-A", cwd=repo)
    await git("commit", "-q", "-m", "start", cwd=repo)
    start = (await git("rev-parse", "HEAD", cwd=repo)).strip()

    # what three failed attempts leave behind
    (repo / "ops.py").write_text("def f():\n    return 999  # attempt 3\n")
    (repo / "scratch.py").write_text("debugging\n")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("expensive to rebuild\n")

    task = Task(spec=spec("t1"), task_start_sha=start)
    res = RunResources(worktree=Worktree(path=repo, branch="main", bare=repo, run_id="r"))
    await _rewind_task(state(), res, task)

    assert (repo / "ops.py").read_text() == "def f():\n    return 1\n"
    assert not (repo / "scratch.py").exists()
    assert (repo / ".venv" / "marker").exists(), "reinstalling the venv costs minutes"


async def test_the_rewind_is_a_no_op_without_a_recorded_start(tmp_path: Path) -> None:
    from orchestrator.nodes import _rewind_task

    res = RunResources(worktree=Worktree(path=tmp_path, branch="m", bare=tmp_path, run_id="r"))
    await _rewind_task(state(), res, Task(spec=spec("t1")))  # no task_start_sha: must not raise
