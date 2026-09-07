from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import contracts  # noqa: F401  (imports every model so __subclasses__ is complete)
from contracts import (
    Budget,
    DebugHypothesis,
    ExecResult,
    Frame,
    ImplementationPlan,
    LLMModel,
    PullRequestDescription,
    RepoProfile,
    ReviewFinding,
    ReviewReport,
    SecurityFinding,
    SecurityReport,
    TaskGraph,
    TaskGraphSpec,
    TaskResult,
    TaskSpec,
    TestFailure,
    TestReport,
    Usage,
)

pytestmark = pytest.mark.unit


def all_llm_models() -> list[type[LLMModel]]:
    seen: set[type[LLMModel]] = set()
    stack: list[type[LLMModel]] = list(LLMModel.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return sorted(seen, key=lambda c: c.__name__)


@pytest.mark.parametrize("model", all_llm_models(), ids=lambda m: m.__name__)
def test_llm_schema_is_strict_and_acyclic(model: type[LLMModel]) -> None:
    schema = model.model_json_schema()
    assert schema.get("additionalProperties") is False, model.__name__
    for name, definition in schema.get("$defs", {}).items():
        assert definition.get("additionalProperties") is False, f"{model.__name__}.$defs.{name}"
        assert f"#/$defs/{name}" not in json.dumps(definition), f"self-reference in {name}"


def test_llm_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RepoProfile.model_validate(
            {
                "languages": ["python"],
                "framework": None,
                "package_manager": "uv",
                "test_command": "pytest",
                "lint_command": None,
                "conventions": [],
                "entry_points": [],
                "surprise": 1,
            }
        )


def test_pytest_does_not_collect_contract_classes() -> None:
    assert TestReport.__test__ is False and TestFailure.__test__ is False


def _spec(task_id: str, *deps: str) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=task_id,
        description="d",
        depends_on=list(deps),
        files=["a.py"],
        acceptance_criteria=["ok"],
        test_selector="tests/",
    )


def test_task_graph_next_ready_follows_dependencies() -> None:
    graph = TaskGraph.from_spec(
        TaskGraphSpec(tasks=[_spec("a"), _spec("b", "a"), _spec("c", "a", "b")])
    )
    assert graph.validate_dag() == []
    order: list[str] = []
    while (task := graph.next_ready()) is not None:
        order.append(task.id)
        task.status = "done"
    assert order == ["a", "b", "c"]
    assert graph.by_id("b").status == "done"
    with pytest.raises(KeyError):
        graph.by_id("zzz")


def test_task_graph_blocked_when_dependency_failed() -> None:
    graph = TaskGraph.from_spec(TaskGraphSpec(tasks=[_spec("a"), _spec("b", "a")]))
    graph.by_id("a").status = "failed"
    assert graph.next_ready() is None


def test_validate_dag_reports_every_problem() -> None:
    graph = TaskGraph(
        tasks=[
            TaskGraph.from_spec(TaskGraphSpec(tasks=[_spec("a", "b")])).tasks[0],
            TaskGraph.from_spec(TaskGraphSpec(tasks=[_spec("b", "a")])).tasks[0],
            TaskGraph.from_spec(TaskGraphSpec(tasks=[_spec("c", "ghost")])).tasks[0],
            TaskGraph.from_spec(TaskGraphSpec(tasks=[_spec("d", "d")])).tasks[0],
            TaskGraph.from_spec(TaskGraphSpec(tasks=[_spec("d")])).tasks[0],
        ]
    )
    problems = graph.validate_dag()
    joined = "\n".join(problems)
    assert "duplicate task id: d" in joined
    assert "task c depends on unknown task ghost" in joined
    assert "task d depends on itself" in joined
    assert any(p.startswith("cycle:") and "a" in p and "b" in p for p in problems)


def test_task_graph_spec_size_limits() -> None:
    with pytest.raises(ValidationError):
        TaskGraphSpec(tasks=[])
    with pytest.raises(ValidationError):
        TaskGraphSpec(tasks=[_spec(f"t{i}") for i in range(9)])


def _samples() -> list[LLMModel]:
    frame = Frame(file="app/x.py", line=10, function="f", code="raise ValueError", in_repo=True)
    failure = TestFailure(
        test_id="tests/test_x.py::test_f",
        kind="exception",
        message="ValueError",
        frames=[frame],
        signature="abc123",
    )
    return [
        RepoProfile(
            languages=["python"],
            framework="fastapi",
            package_manager="uv",
            test_command="pytest -q",
            lint_command="ruff check .",
            conventions=["one router per file"],
            entry_points=["app/main.py"],
        ),
        ImplementationPlan(
            approach="a",
            affected_files=["a.py"],
            new_files=[],
            risks=[],
            test_strategy="pytest",
            open_questions=[],
        ),
        TaskGraphSpec(tasks=[_spec("a"), _spec("b", "a")]),
        TestReport(
            passed=False,
            total=3,
            failed=1,
            errors=0,
            skipped=0,
            failures=[failure],
            duration_s=1.5,
            command="pytest -q",
            truncated_output="...",
        ),
        DebugHypothesis(failure_class="exception", root_cause="x", plan="y", confidence=0.7),
        ReviewReport(
            findings=[
                ReviewFinding(
                    file="a.py",
                    line=3,
                    severity="blocking",
                    category="correctness",
                    summary="s",
                    failure_scenario="f",
                )
            ],
            blocking=True,
        ),
        SecurityReport(
            findings=[
                SecurityFinding(
                    tool="bandit",
                    rule="B101",
                    file="a.py",
                    line=1,
                    severity="low",
                    message="m",
                    verified_by_llm=True,
                    false_positive=False,
                    rationale="r",
                )
            ],
            critical=False,
            checklist={"no_secrets": True},
        ),
        PullRequestDescription(
            title="feat: x",
            summary="s",
            changes=["c"],
            testing="t",
            known_issues=[],
            rollback="revert",
        ),
        TaskResult(
            summary="done", files_touched=["a.py"], how_to_test="pytest", notes_for_reviewer=[]
        ),
    ]


@pytest.mark.parametrize("sample", _samples(), ids=lambda m: type(m).__name__)
def test_round_trip_json(sample: LLMModel) -> None:
    assert type(sample).model_validate_json(sample.model_dump_json()) == sample


def test_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        DebugHypothesis(failure_class="flaky", root_cause="x", plan="y", confidence=1.5)


def test_usage_add_and_totals() -> None:
    a = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=100, cost_usd=0.5)
    b = Usage(input_tokens=1, output_tokens=1, cache_write_tokens=7, cost_usd=0.25, wall_clock_s=3)
    c = a.add(b)
    assert (c.input_tokens, c.output_tokens, c.cache_read_tokens, c.cache_write_tokens) == (
        11,
        6,
        100,
        7,
    )
    assert c.cost_usd == 0.75 and c.wall_clock_s == 3 and c.total_tokens == 124
    assert a == Usage(
        input_tokens=10, output_tokens=5, cache_read_tokens=100, cost_usd=0.5
    )  # unchanged


def test_budget_defaults_and_validation() -> None:
    assert Budget().max_debug_attempts == 3 and Budget().wall_clock_s == 45 * 60
    with pytest.raises(ValidationError):
        Budget(max_usd=0)


def test_exec_result_ok() -> None:
    assert ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1).ok
    assert not ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1, timed_out=True).ok
    assert not ExecResult(exit_code=1, stdout="", stderr="", duration_ms=1).ok
