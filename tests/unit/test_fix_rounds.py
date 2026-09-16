"""Fix rounds: a blocking review sends the change back, twice at most, then says so.

The thing worth testing is the budget, and specifically that **one place owns it**. The
phase document checked the rounds in `review_node` and again in `transition`, with `<` in
one and `<=` in the other; it happened to work because a third condition covered the gap.
Here the waiting fix task *is* the grant, so the two cannot disagree — and these tests are
what would catch it if they ever did.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from contracts import Budget, ReviewFinding, ReviewReport, Task, TaskGraph, TaskSpec
from orchestrator import nodes
from orchestrator.state import Phase, RunState
from orchestrator.transition import transition

pytestmark = pytest.mark.unit


def finding(severity: str = "blocking", line: int = 9, file: str = "src/a.py") -> ReviewFinding:
    return ReviewFinding(
        file=file,
        line=line,
        severity=cast("Any", severity),
        category="off-by-one",
        summary="drops the last page",
        failure_scenario="paginate([1,2,3], 2) returns [[1,2]]",
    )


def spec(task_id: str) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title="t",
        description="d",
        depends_on=[],
        files=["src/a.py"],
        acceptance_criteria=["works"],
        test_selector="tests/",
    )


def state_after_review(**kw: Any) -> RunState:
    tasks = TaskGraph(tasks=[Task(spec=spec("t1"), status="done")])
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "goal": "g",
        "repo_url": "https://github.com/a/b",
        "base_branch": "main",
        "work_branch": "agent/x",
        "phase": Phase.REVIEW,
        "tasks": tasks,
        "budget": Budget(max_fix_rounds=2),
    }
    return RunState(**{**base, **kw})


class FakeDeps:
    def __init__(self) -> None:
        self.engine = None


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    class NullSession:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def noop(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(nodes, "session", lambda _engine: NullSession())
    monkeypatch.setattr("storage.repo.upsert_tasks", noop)


async def grant(s: RunState, report: ReviewReport) -> int | None:
    return await nodes._grant_fix_round(s, cast("Any", FakeDeps()), report)


# ---- the budget --------------------------------------------------------------------


async def test_a_blocking_review_buys_a_round() -> None:
    s = state_after_review()
    report = ReviewReport(findings=[finding()], blocking=True)

    assert await grant(s, report) == 1
    assert s.fix_rounds["review"] == 1
    assert s.return_to is Phase.REVIEW
    assert s.tasks is not None
    fix = s.tasks.tasks[-1]
    assert fix.id == "fix-review-1" and fix.kind == "fix"
    assert fix.status == "pending", "so next_ready() finds it and transition can see the grant"


async def test_a_clean_review_buys_nothing() -> None:
    s = state_after_review()
    assert (
        await grant(s, ReviewReport(findings=[finding(severity="minor")], blocking=False)) is None
    )
    assert s.fix_rounds == {} and s.return_to is None
    assert s.tasks is not None and len(s.tasks.tasks) == 1


async def test_the_second_round_is_granted_and_the_third_is_not() -> None:
    """Two rounds, then the findings go on the record and the run proceeds."""
    s = state_after_review()
    report = ReviewReport(findings=[finding()], blocking=True)

    assert await grant(s, report) == 1
    s.tasks.tasks[-1].status = "done"  # type: ignore[union-attr]
    assert await grant(s, report) == 2
    s.tasks.tasks[-1].status = "done"  # type: ignore[union-attr]

    assert await grant(s, report) is None, "the budget is two"
    assert s.fix_rounds["review"] == 2
    assert len(s.known_issues) == 1
    assert "src/a.py:9" in s.known_issues[0]
    assert s.tasks is not None and len(s.tasks.tasks) == 3, "no third fix task"


async def test_a_budget_of_zero_never_grants_a_round() -> None:
    """Configurable, and the configuration is respected rather than assumed."""
    s = state_after_review(budget=Budget(max_fix_rounds=0))
    assert await grant(s, ReviewReport(findings=[finding()], blocking=True)) is None
    assert s.known_issues, "and the finding is still reported"


async def test_only_findings_worth_a_round_are_sent_back() -> None:
    s = state_after_review()
    report = ReviewReport(
        findings=[finding(severity="blocking"), finding(severity="nit", line=99)], blocking=True
    )

    await grant(s, report)

    assert s.tasks is not None
    body = s.tasks.tasks[-1].spec.description
    assert "line 9" in body and "line 99" not in body, "a nit does not cost a coding round"


# ---- the loop the budget bounds ------------------------------------------------------


async def test_the_whole_loop_from_blocking_review_to_pull_request() -> None:
    """REVIEW → CODE → TEST → REVIEW → PR, walked with the real transition function.

    The point is the hand-off: each phase's decision is made by the code that owns it, and
    they have to agree without sharing a counter.
    """
    from tests.unit.test_transition import report as test_report

    s = state_after_review()
    blocking = ReviewReport(findings=[finding()], blocking=True)
    s.review = blocking

    # first review: blocking, a round is bought, so the machine goes back to coding
    assert await grant(s, blocking) == 1
    assert transition(s) == Phase.CODE

    # the fix task passes its tests, and TEST returns to the gate that asked
    s.phase = Phase.TEST
    s.last_test_report = test_report(True)
    s.tasks.tasks[-1].status = "done"  # type: ignore[union-attr]
    assert transition(s) == Phase.REVIEW
    assert s.return_to is None, "the hop is spent"

    # second review: clean this time
    s.phase = Phase.REVIEW
    clean = ReviewReport(findings=[], blocking=False)
    s.review = clean
    assert await grant(s, clean) is None
    assert transition(s) == Phase.PR
    assert s.known_issues == [], "nothing was left unresolved"


async def test_a_run_that_never_stops_blocking_still_reaches_a_pull_request() -> None:
    """Two rounds, still blocking, and the run ends with an honest draft rather than a
    loop. The alternative — refusing to finish — produces nothing anybody can read."""
    from tests.unit.test_transition import report as test_report

    s = state_after_review()
    blocking = ReviewReport(findings=[finding()], blocking=True)

    for expected in (1, 2):
        s.phase = Phase.REVIEW
        s.review = blocking
        assert await grant(s, blocking) == expected
        assert transition(s) == Phase.CODE
        s.phase = Phase.TEST
        s.last_test_report = test_report(True)
        s.tasks.tasks[-1].status = "done"  # type: ignore[union-attr]
        assert transition(s) == Phase.REVIEW

    s.phase = Phase.REVIEW
    s.review = blocking
    assert await grant(s, blocking) is None
    assert transition(s) == Phase.PR
    assert s.known_issues, "and it says what it gave up on"
