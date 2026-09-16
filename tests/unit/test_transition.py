"""The transition table. This is the closest thing the project has to a specification of
its own behaviour, so it gets more rows than any other module gets tests.

`transition` is a function of `RunState` alone: no I/O, nothing but `s`. That is what
makes a table possible, and the table is what makes the verification loop reviewable
without running a model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from contracts import (
    Budget,
    ImplementationPlan,
    RepoProfile,
    Task,
    TaskGraph,
    TaskResult,
    TaskSpec,
    TestReport,
    Usage,
)
from orchestrator.state import TERMINAL, Phase, RunState
from orchestrator.transition import MAX_DEBUG_ATTEMPTS, transition

pytestmark = pytest.mark.unit

PROFILE = RepoProfile(
    languages=["python"],
    framework=None,
    package_manager="uv",
    test_command="pytest -q",
    lint_command=None,
    conventions=[],
    entry_points=[],
)
PLAN_OK = ImplementationPlan(
    approach="do it",
    affected_files=["a.py"],
    new_files=[],
    risks=[],
    test_strategy="pytest",
    open_questions=[],
)
PLAN_ASKS = PLAN_OK.model_copy(update={"open_questions": ["which scheme?"]})
RESULT = TaskResult(
    summary="done", files_touched=["a.py"], how_to_test="pytest", notes_for_reviewer=[]
)


def spec(task_id: str) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=f"task {task_id}",
        description="d",
        depends_on=[],
        files=["a.py"],
        acceptance_criteria=["works"],
        test_selector="tests/",
    )


def graph(*ids: str) -> TaskGraph:
    return TaskGraph(tasks=[Task(spec=spec(i)) for i in ids])


def report(passed: bool, signature: str = "sig-a") -> TestReport:
    return TestReport(
        passed=passed,
        total=1,
        failed=0 if passed else 1,
        errors=0,
        skipped=0,
        failures=[],
        duration_s=0.1,
        command="pytest -q",
        truncated_output="",
        signature=signature,
    )


def state(**kw: Any) -> RunState:
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "goal": "g",
        "repo_url": "https://github.com/a/b",
        "base_branch": "main",
        "work_branch": "agent/x",
        "started_at": datetime.now(UTC),
    }
    return RunState(**{**base, **kw})


# ---- the linear spine --------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "kw", "expected"),
    [
        (Phase.SETUP, {}, Phase.ANALYZE),
        (Phase.ANALYZE, {"repo": PROFILE}, Phase.PLAN),
        (Phase.ANALYZE, {}, Phase.FAILED),
        (Phase.PLAN, {"plan": PLAN_OK}, Phase.DECOMPOSE),
        (Phase.PLAN, {"plan": PLAN_ASKS}, Phase.AWAITING_INPUT),
        (Phase.PLAN, {}, Phase.FAILED),
        (Phase.DECOMPOSE, {"tasks": graph("t1")}, Phase.CODE),
        (Phase.DECOMPOSE, {}, Phase.FAILED),
        (Phase.DEBUG, {}, Phase.TEST),
        (Phase.PR, {"pr_url": "https://example/pull/1"}, Phase.DONE),
        (Phase.PR, {}, Phase.FAILED),
    ],
)
def test_linear_edges(current: Phase, kw: dict[str, Any], expected: Phase) -> None:
    assert transition(state(phase=current, **kw)) == expected


# ---- CODE -------------------------------------------------------------------------


def test_code_with_a_result_goes_to_test() -> None:
    s = state(
        phase=Phase.CODE,
        tasks=graph("t1"),
        current_task_id="t1",
        task_results={"t1": RESULT},
    )
    assert transition(s) == Phase.TEST


def test_code_without_a_result_escalates_rather_than_failing() -> None:
    """A Coder that produced nothing is one failed attempt; ESCALATE decides if it retries."""
    s = state(phase=Phase.CODE, tasks=graph("t1"), current_task_id="t1")
    assert transition(s) == Phase.ESCALATE
    assert s.escalation_reason == "coder_no_result"


# ---- TEST: the verification loop ---------------------------------------------------


def test_a_passing_test_moves_to_the_next_task() -> None:
    tasks = graph("t1", "t2")
    tasks.by_id("t1").status = "done"
    s = state(phase=Phase.TEST, tasks=tasks, last_test_report=report(True))
    assert transition(s) == Phase.CODE


def test_a_passing_test_with_no_work_left_goes_to_review() -> None:
    """Every task done means the change is final, so it is reviewed before it is pushed."""
    tasks = graph("t1")
    tasks.by_id("t1").status = "done"
    s = state(phase=Phase.TEST, tasks=tasks, last_test_report=report(True))
    assert transition(s) == Phase.REVIEW


def test_review_leads_to_the_pull_request() -> None:
    """Advisory for now: findings are recorded and the run proceeds. Fix rounds are next,
    and this row changes when they land — which is the point of having it."""
    assert transition(state(phase=Phase.REVIEW)) == Phase.PR


def test_passing_clears_the_debug_memory() -> None:
    """The next task's first failure is its own, not a continuation of this one."""
    tasks = graph("t1", "t2")
    tasks.by_id("t1").status = "done"
    s = state(
        phase=Phase.TEST,
        tasks=tasks,
        last_test_report=report(True),
        previous_failure_signature="sig-a",
        strategy="alternative",
    )
    assert transition(s) == Phase.CODE
    assert s.previous_failure_signature is None and s.strategy is None


@pytest.mark.parametrize("attempts", [0, 1, 2])
def test_a_failing_test_debugs_while_attempts_remain(attempts: int) -> None:
    s = state(
        phase=Phase.TEST,
        tasks=graph("t1"),
        current_task_id="t1",
        attempts={"t1": attempts},
        last_test_report=report(False),
    )
    assert transition(s) == Phase.DEBUG
    assert s.escalation_reason is None


def test_a_failing_test_escalates_once_attempts_are_exhausted() -> None:
    s = state(
        phase=Phase.TEST,
        tasks=graph("t1"),
        current_task_id="t1",
        attempts={"t1": MAX_DEBUG_ATTEMPTS},
        last_test_report=report(False),
    )
    assert transition(s) == Phase.ESCALATE
    assert s.escalation_reason == "debug_attempts_exhausted"


def test_the_same_signature_twice_asks_for_an_alternative_strategy() -> None:
    """Without this the Debugger forms the same theory three times and calls it three tries."""
    s = state(
        phase=Phase.TEST,
        tasks=graph("t1"),
        current_task_id="t1",
        attempts={"t1": 1},
        last_test_report=report(False, "sig-a"),
        previous_failure_signature="sig-a",
    )
    assert transition(s) == Phase.DEBUG
    assert s.strategy == "alternative"


def test_a_different_signature_means_progress_and_clears_the_strategy() -> None:
    s = state(
        phase=Phase.TEST,
        tasks=graph("t1"),
        current_task_id="t1",
        attempts={"t1": 1},
        last_test_report=report(False, "sig-b"),
        previous_failure_signature="sig-a",
        strategy="alternative",
    )
    assert transition(s) == Phase.DEBUG
    assert s.strategy is None
    assert s.previous_failure_signature == "sig-b"


def test_a_missing_report_escalates_instead_of_guessing() -> None:
    s = state(phase=Phase.TEST, tasks=graph("t1"), current_task_id="t1")
    assert transition(s) == Phase.ESCALATE
    assert s.escalation_reason == "no_test_report"


# ---- the two guards ahead of the machine -------------------------------------------


@pytest.mark.parametrize(
    "phase",
    [Phase.SETUP, Phase.ANALYZE, Phase.PLAN, Phase.DECOMPOSE, Phase.CODE, Phase.TEST, Phase.DEBUG],
)
def test_cancelled_outranks_every_phase(phase: Phase) -> None:
    assert transition(state(phase=phase, cancelled=True)) == Phase.FAILED


@pytest.mark.parametrize(
    "phase", [Phase.SETUP, Phase.ANALYZE, Phase.PLAN, Phase.CODE, Phase.TEST, Phase.DEBUG]
)
def test_an_exhausted_budget_escalates_from_every_phase(phase: Phase) -> None:
    s = state(phase=phase, budget=Budget(max_usd=1.0), usage=Usage(cost_usd=1.0))
    assert transition(s) == Phase.ESCALATE
    assert s.escalation_reason == "budget_usd"


@pytest.mark.parametrize(
    ("budget", "usage", "elapsed_ago", "reason"),
    [
        (Budget(max_usd=1.0), Usage(cost_usd=1.0), 0, "budget_usd"),
        (Budget(wall_clock_s=60), Usage(), 120, "budget_wall_clock"),
        (Budget(max_tokens=100), Usage(input_tokens=100), 0, "budget_tokens"),
    ],
)
def test_each_budget_names_itself(
    budget: Budget, usage: Usage, elapsed_ago: int, reason: str
) -> None:
    s = state(
        phase=Phase.TEST,
        budget=budget,
        usage=usage,
        started_at=datetime.now(UTC) - timedelta(seconds=elapsed_ago),
    )
    assert transition(s) == Phase.ESCALATE
    assert s.escalation_reason == reason


def test_cancelled_beats_an_exhausted_budget() -> None:
    """Both are true; the human's decision is the one that gets honoured."""
    s = state(
        phase=Phase.TEST, cancelled=True, budget=Budget(max_usd=1.0), usage=Usage(cost_usd=5.0)
    )
    assert transition(s) == Phase.FAILED
    assert s.escalation_reason is None


def test_time_parked_on_a_human_does_not_spend_the_wall_clock_budget() -> None:
    s = state(
        phase=Phase.TEST,
        budget=Budget(wall_clock_s=60),
        started_at=datetime.now(UTC) - timedelta(seconds=600),
        waiting_s=600,
        tasks=graph("t1"),
        current_task_id="t1",
        last_test_report=report(False),
    )
    assert transition(s) == Phase.DEBUG, "the run waited; it did not work for ten minutes"


def test_an_unmeasurable_cost_does_not_look_like_an_unspent_budget() -> None:
    """With no price, the dollar budget is dropped — but wall clock still bounds the run."""
    spent = state(
        phase=Phase.TEST,
        cost_measurable=False,
        budget=Budget(max_usd=1.0, wall_clock_s=60),
        usage=Usage(input_tokens=10_000_000),
        started_at=datetime.now(UTC) - timedelta(seconds=120),
    )
    assert transition(spent) == Phase.ESCALATE
    assert spent.escalation_reason == "budget_wall_clock"


# ---- resuming ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resume", "expected"),
    [(Phase.PLAN, Phase.PLAN), (Phase.DEBUG, Phase.DEBUG), (None, Phase.PLAN)],
)
def test_awaiting_input_resumes_where_it_was_asked_from(
    resume: Phase | None, expected: Phase
) -> None:
    assert transition(state(phase=Phase.AWAITING_INPUT, resume_phase=resume)) == expected


@pytest.mark.parametrize(
    ("resume", "expected"),
    [
        (Phase.CODE, Phase.CODE),
        (Phase.AWAITING_INPUT, Phase.AWAITING_INPUT),
        (Phase.FAILED, Phase.FAILED),
        (None, Phase.FAILED),
    ],
)
def test_escalate_goes_where_escalate_node_decided(resume: Phase | None, expected: Phase) -> None:
    assert transition(state(phase=Phase.ESCALATE, resume_phase=resume)) == expected


# ---- the loop as a whole -----------------------------------------------------------


def test_three_failures_then_escalation_is_the_whole_debug_budget() -> None:
    """What the phase is for, walked end to end without a model."""
    tasks = graph("t1")
    s = state(
        phase=Phase.TEST,
        tasks=tasks,
        current_task_id="t1",
        last_test_report=report(False, "sig-a"),
    )
    seen = []
    for attempt in range(MAX_DEBUG_ATTEMPTS):
        s.attempts["t1"] = attempt
        seen.append(transition(s))
        assert s.phase is Phase.TEST  # transition does not move the run; the runner does
    assert seen == [Phase.DEBUG] * MAX_DEBUG_ATTEMPTS
    # the signature never changed, so every attempt after the first asked for a new angle
    assert s.strategy == "alternative"

    s.attempts["t1"] = MAX_DEBUG_ATTEMPTS
    assert transition(s) == Phase.ESCALATE
    assert s.escalation_reason == "debug_attempts_exhausted"


# Phases with no transition row yet. Each one is a phase the enum declares and the machine
# cannot leave, so the list shrinking is how this phase's progress shows up here.
UNROUTED = {Phase.SECURITY}


def test_every_phase_is_routed_except_the_ones_not_built_yet() -> None:
    """Named rather than sampled: picking one unbuilt phase as the example meant moving the
    test every time one was built, and a test that moves stops guarding anything."""
    for phase in Phase:
        if phase in TERMINAL:
            continue  # terminal phases are never handed to transition
        s = state(phase=phase, tasks=graph("t1"), last_test_report=report(True))
        if phase in UNROUTED:
            with pytest.raises(ValueError, match="no transition"):
                transition(s)
        else:
            assert isinstance(transition(s), Phase), phase
