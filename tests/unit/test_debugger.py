"""The Debugger: the gate that enforces diagnosis-before-edit, and the prompt's evidence.

The gate is the point of the step. A hypothesis submitted after the edit describes the
edit, so a model that reaches for `bash` first must be refused and told why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agents.debugger import (
    ALTERNATIVE_BLOCK,
    HYPOTHESIS_KEY,
    debug_message,
    render_attempts,
    render_report,
)
from contracts import DebugHypothesis, Frame, TaskSpec, TestFailure, TestReport
from orchestrator.hooks import OrchestratorHooks

pytestmark = pytest.mark.unit

TASK = TaskSpec(
    id="t1",
    title="paginate items",
    description="split a list into pages",
    depends_on=[],
    files=["fixture/ops.py"],
    acceptance_criteria=["the last partial page is returned"],
    test_selector="tests/test_ops.py",
)


def failing_report(*, in_repo: bool = True) -> TestReport:
    fail = TestFailure(
        test_id="tests/test_ops.py::test_last_page",
        kind="assertion",
        message="AssertionError: assert [[1, 2]] == [[1, 2], [3]]",
        frames=[
            Frame(
                file="tests/test_ops.py",
                line=5,
                function="test_last_page",
                code="assert paginate([1, 2, 3], 2) == [[1, 2], [3]]",
                in_repo=in_repo,
            ),
            Frame(
                file="fixture/ops.py",
                line=3,
                function="paginate",
                code="for i in range(0, len(items) - 1, size):",
                in_repo=in_repo,
            ),
            Frame(
                file=".venv/lib/pytest/python.py",
                line=2,
                function="pytest_pyfunc_call",
                code="",
                in_repo=False,
            ),
        ],
        signature="sig-a",
    )
    return TestReport(
        passed=False,
        total=3,
        failed=1,
        errors=0,
        skipped=0,
        failures=[fail],
        duration_s=0.2,
        command="pytest -q tests/test_ops.py",
        truncated_output="",
        signature="report-a",
    )


def hypothesis(cause: str = "the stop bound excludes the last page") -> DebugHypothesis:
    return DebugHypothesis(
        failure_class="assertion", root_cause=cause, plan="widen the bound", confidence=0.8
    )


def hooks(role: str, submitted: dict[str, Any] | None = None) -> OrchestratorHooks:
    return OrchestratorHooks(
        run_id=uuid4(),
        step_id=uuid4(),
        engine=None,
        bus=None,
        provider_name="test",
        model="test/model",
        effort=None,
        role=role,
        submitted=submitted if submitted is not None else {},
    )


# ---- the gate ----------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["bash", "str_replace_based_edit_tool", "run_tests", "git_commit"])
async def test_a_mutating_tool_is_refused_before_the_hypothesis(tool: str) -> None:
    denial = await hooks("debugger").before_tool(tool, {})
    assert denial is not None
    assert "submit_hypothesis first" in denial
    assert tool in denial, "the refusal should name what it refused"


@pytest.mark.parametrize("tool", ["read_file", "search_code", "git_status", "git_diff"])
async def test_reading_is_always_allowed(tool: str) -> None:
    """The gate demands evidence, so it cannot block the tools that gather it."""
    assert await hooks("debugger").before_tool(tool, {}) is None


async def test_submitting_the_hypothesis_is_not_itself_blocked() -> None:
    """It is non-mutating, which is what stops the gate closing on its own key."""
    assert await hooks("debugger").before_tool("submit_hypothesis", {}) is None


@pytest.mark.parametrize("tool", ["bash", "str_replace_based_edit_tool", "run_tests", "git_commit"])
async def test_everything_opens_once_the_hypothesis_exists(tool: str) -> None:
    submitted = {HYPOTHESIS_KEY: hypothesis()}
    assert await hooks("debugger", submitted).before_tool(tool, {}) is None


async def test_the_gate_sees_a_submission_made_mid_loop() -> None:
    """The hooks hold `ctx.submitted` by reference, so the gate opens without a rebuild."""
    submitted: dict[str, Any] = {}
    h = hooks("debugger", submitted)
    assert await h.before_tool("bash", {}) is not None
    submitted[HYPOTHESIS_KEY] = hypothesis()  # as the submit tool would
    assert await h.before_tool("bash", {}) is None


@pytest.mark.parametrize("role", ["coder", "analyzer", ""])
async def test_the_gate_applies_only_to_the_debugger(role: str) -> None:
    assert await hooks(role).before_tool("str_replace_based_edit_tool", {}) is None


async def test_an_unknown_tool_is_not_gated_here() -> None:
    """The provider already rejects unknown tools; the gate must not mask that."""
    assert await hooks("debugger").before_tool("no_such_tool", {}) is None


# ---- the evidence in the prompt ----------------------------------------------------


def test_the_report_renders_in_repo_frames_with_their_source() -> None:
    out = render_report(failing_report())
    assert "tests/test_ops.py::test_last_page" in out
    assert "kind: `assertion`" in out
    assert "fixture/ops.py:3 in `paginate`" in out
    assert "for i in range(0, len(items) - 1, size):" in out
    # a vendored frame is not the agent's to fix and is left out
    assert ".venv" not in out


def test_source_context_overrides_the_single_frame_line_when_available() -> None:
    context = {"tests/test_ops.py::test_last_page|fixture/ops.py:3": "> 3 | the wider view"}
    out = render_report(failing_report(), context)
    assert "the wider view" in out


def test_a_failure_with_no_repository_frames_says_so() -> None:
    out = render_report(failing_report(in_repo=False))
    assert "no frames inside the repository" in out


def test_previous_attempts_render_in_order_with_their_outcome() -> None:
    out = render_attempts([hypothesis("first theory"), hypothesis("second theory")])
    assert out.index("Attempt 1") < out.index("Attempt 2")
    assert "first theory" in out and "second theory" in out
    assert "the tests still failed" in out


def test_no_previous_attempts_renders_nothing() -> None:
    assert render_attempts([]) == ""


def test_the_alternative_block_appears_only_when_the_strategy_is_set() -> None:
    report = failing_report()
    plain = debug_message("goal", TASK, report)
    assert ALTERNATIVE_BLOCK not in plain

    nudged = debug_message("goal", TASK, report, alternative=True)
    assert ALTERNATIVE_BLOCK in nudged
    # the demand that makes the block worth having, matched without crossing a wrap
    assert "off the table" in nudged and "not restate it in different words" in nudged


def test_a_human_hint_is_carried_into_the_prompt() -> None:
    out = debug_message(
        "goal", TASK, failing_report(), human_hint="the API returns 1-indexed pages"
    )
    assert "A human left this note" in out and "1-indexed" in out


def test_the_message_always_carries_the_task_and_its_criteria() -> None:
    out = debug_message("build pagination", TASK, failing_report())
    assert "build pagination" in out
    assert "Task t1: paginate items" in out
    assert "the last partial page is returned" in out
    assert "submit_hypothesis" in out


def test_repository_content_is_fenced_as_untrusted() -> None:
    """A comment in a test file is not an instruction, and the fence says so."""
    out = render_report(failing_report())
    assert "untrusted" in out.lower() or "```" in out


def test_many_failures_are_capped_with_a_count(tmp_path: Path) -> None:
    from agents.debugger import MAX_FAILURES_SHOWN

    report = failing_report()
    many = report.model_copy(
        update={"failures": report.failures * (MAX_FAILURES_SHOWN + 2), "failed": 8}
    )
    out = render_report(many)
    assert "more failures" in out


async def test_the_step_enforces_the_tool_whose_absence_is_fatal() -> None:
    """`must_call` takes one tool and this step has two, so it has to be the right one.

    `run` raises when the hypothesis is missing — "a failed attempt rather than a partial
    one" — and explicitly tolerates a missing result, because the hypothesis is still worth
    recording and the next TEST decides whether the edits helped. Enforcing the result
    therefore guarded the outcome the step survives and left the one that kills it to
    chance.

    Measured on a Django scale run: the loop forced `submit_result` twice, succeeded both
    times, and the step still died with "debugger did not submit a hypothesis". Every
    reminder and the forced call were spent on the tool that did not need them.
    """
    import inspect

    from agents import debugger as mod

    source = inspect.getsource(mod.DebuggerAgent.run)

    assert "must_call=hypothesis_tool.name" in source
    assert "must_call=result_tool.name" not in source, (
        "the enforced tool is the survivable one again"
    )


async def test_a_missing_result_is_still_survivable() -> None:
    """The other half, and the reason the choice above is not arbitrary. If both were
    fatal, which one `must_call` names would not matter."""
    import inspect

    from agents import debugger as mod

    source = inspect.getsource(mod.DebuggerAgent.run)
    after_result = source[source.index("result = ctx.submitted.get(RESULT_KEY)") :]

    assert "raise" not in after_result, "a missing result must not raise"
    assert "result if isinstance(result, TaskResult) else None" in after_result
