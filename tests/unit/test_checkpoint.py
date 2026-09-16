"""RunState must survive the JSON round-trip a checkpoint puts it through."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from contracts import (
    Budget,
    ImplementationPlan,
    RepoFacts,
    RepoProfile,
    TaskGraph,
    TaskGraphSpec,
    TaskResult,
    TaskSpec,
    TestReport,
    Usage,
)
from orchestrator.state import Phase, RunState

pytestmark = pytest.mark.unit


def full_state() -> RunState:
    """Every optional field populated: the round-trip must lose none of them."""
    spec = TaskSpec(
        id="t1",
        title="add stats",
        description="d",
        depends_on=[],
        files=["fixture/stats.py"],
        acceptance_criteria=["mean works"],
        test_selector="tests/test_stats.py",
    )
    return RunState(
        run_id=uuid.uuid4(),
        goal="add a Stats class",
        repo_url="https://github.com/acme/demo",
        base_branch="main",
        work_branch="agent/x",
        phase=Phase.TEST,
        base_sha="a" * 40,
        unattended=True,
        test_command="uv run --no-sync pytest -q",
        facts=RepoFacts(languages=["python"], test_command="pytest -q", detected_by="pyproject"),
        repo=RepoProfile(
            languages=["python"],
            framework=None,
            package_manager="uv",
            test_command="pytest -q",
            lint_command=None,
            conventions=["tests mirror packages"],
            entry_points=["fixture/__main__.py"],
        ),
        plan=ImplementationPlan(
            approach="a",
            affected_files=["x.py"],
            new_files=["y.py"],
            risks=["r"],
            test_strategy="pytest",
            open_questions=["q?"],
        ),
        answers=[("which scheme?", "JWT")],
        tasks=TaskGraph.from_spec(TaskGraphSpec(tasks=[spec])),
        current_task_id="t1",
        attempts={"t1": 2},
        task_results={
            "t1": TaskResult(
                summary="done", files_touched=["x.py"], how_to_test="pytest", notes_for_reviewer=[]
            )
        },
        last_test_report=TestReport(
            passed=True,
            total=3,
            failed=0,
            errors=0,
            skipped=0,
            failures=[],
            duration_s=0.4,
            command="pytest -q",
            truncated_output="",
        ),
        pr_url="https://github.com/acme/demo/pull/1",
        pushed=True,
        budget=Budget(max_usd=5),
        usage=Usage(input_tokens=100, output_tokens=20, cost_usd=0.01),
        waiting_s=12.5,
        seq=7,
        started_at=datetime.now(UTC),
        previous_failure_signature="abc1230000000000",
        strategy="alternative",
        escalation_reason="debug_attempts_exhausted",
        baseline_failures={"sig1", "sig2"},
        resume_phase=Phase.DEBUG,
        test_context={"tests/test_ops.py::test_x": "> 3 | assert False"},
        warned={"budget_usd"},
        flaky_tests={"tests/test_slow.py::test_sometimes"},
        preexisting_failures={"tests/test_legacy.py::test_old"},
        fix_rounds={"review": 1},
        return_to=Phase.REVIEW,
        known_issues=["[major] src/a.py:9 — size is not checked (fails when: size=0)"],
    )


def test_round_trip_preserves_every_field() -> None:
    """UUIDs, datetimes and the tuples in `answers` are the usual casualties."""
    original = full_state()
    restored = RunState.model_validate(original.model_dump(mode="json"))
    assert restored.model_dump() == original.model_dump()
    assert restored.run_id == original.run_id
    assert restored.started_at == original.started_at
    assert restored.answers == [("which scheme?", "JWT")]
    assert restored.attempts == {"t1": 2}
    assert restored.tasks is not None and restored.tasks.by_id("t1").spec.title == "add stats"
    # Phase 3 added a set and an enum, both of which JSON flattens
    assert restored.baseline_failures == {"sig1", "sig2"}
    assert restored.warned == {"budget_usd"}
    assert restored.resume_phase is Phase.DEBUG
    assert restored.strategy == "alternative"
    assert restored.flaky_tests == {"tests/test_slow.py::test_sometimes"}
    assert restored.preexisting_failures == {"tests/test_legacy.py::test_old"}
    # Phase 4's fields are here already, so a checkpoint written today still loads then
    assert restored.fix_rounds == {"review": 1} and restored.return_to is Phase.REVIEW
    assert restored.known_issues == [
        "[major] src/a.py:9 — size is not checked (fails when: size=0)"
    ], "a run resumed mid-fix-round must still know what it gave up on"


def test_derived_task_properties_follow_the_graph() -> None:
    s = full_state()
    assert s.task is not None and s.task.id == "t1"
    assert s.task_result is not None and s.task_result.summary == "done"

    s.current_task_id = "nope"  # a stale id must not raise
    assert s.task is None and s.task_result is None

    s.current_task_id = None
    assert s.task is None


def test_a_minimal_state_round_trips_too() -> None:
    s = RunState(
        run_id=uuid.uuid4(),
        goal="g",
        repo_url="https://github.com/a/b",
        base_branch="main",
        work_branch="agent/y",
    )
    restored = RunState.model_validate(s.model_dump(mode="json"))
    assert restored.model_dump() == s.model_dump()
    assert restored.phase is Phase.SETUP and restored.seq == 0
