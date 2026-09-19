"""Keeping a long loop inside its context window, and inside its budget.

Two mechanisms, both host-side because this build has no Anthropic provider to ask for the
server-side versions the phase document specifies.

The interesting constraint on the first is that it must not *lose* anything the model
depends on. Clearing a tool result is cheap; clearing the record that the call happened is
how a loop ends up running `ls` for the fifth time. So the assistant's `tool_calls` are
untouched, and the cleared body is replaced by a line saying it was cleared rather than
removed.
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts import Budget, Usage
from gateway import context
from gateway.openai_compat_provider import _land_it, _over_task_budget
from gateway.provider import Request

pytestmark = pytest.mark.unit


def transcript(results: int, *, size: int = 2_000) -> list[dict[str, Any]]:
    """A loop's worth of messages: a call and a large result per turn."""
    out: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    for i in range(results):
        out.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'},
                    }
                ],
            }
        )
        out.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * size})
    return out


def bodies(messages: list[dict[str, Any]]) -> list[str]:
    return [str(m["content"]) for m in messages if m.get("role") == "tool"]


# ---- what is cleared ----------------------------------------------------------------------


def test_old_results_go_and_recent_ones_stay() -> None:
    """The recent ones are what the model is working from right now."""
    messages = transcript(20)

    cleared = context.clear_old_tool_results(messages, keep_recent=6)

    assert cleared == 14
    assert bodies(messages)[-6:] == ["x" * 2_000] * 6
    assert set(bodies(messages)[:14]) == {context.CLEARED}


def test_the_record_that_the_call_happened_is_never_touched() -> None:
    """The Debugger reads its own earlier commands to know what it has already tried. A
    loop that cannot remember what it ran runs it again."""
    messages = transcript(20)

    context.clear_old_tool_results(messages, keep_recent=2)

    calls = [m for m in messages if m.get("tool_calls")]
    assert len(calls) == 20
    assert all(c["tool_calls"][0]["function"]["arguments"] for c in calls)


def test_a_cleared_result_says_it_was_cleared() -> None:
    """A tool result that silently vanishes reads as a call that never happened, and the
    model makes it again — which costs more than the clearing saved."""
    messages = transcript(10)

    context.clear_old_tool_results(messages, keep_recent=1)

    assert "cleared" in bodies(messages)[0]
    assert "call the tool again" in bodies(messages)[0]


def test_a_small_result_is_not_worth_a_placeholder() -> None:
    messages = transcript(20, size=50)

    assert context.clear_old_tool_results(messages, keep_recent=2) == 0
    assert bodies(messages) == ["x" * 50] * 20


def test_clearing_twice_clears_nothing_the_second_time() -> None:
    """Every clear moves the cached prefix and costs a cache write. Doing it again for no
    benefit would spend that every turn."""
    messages = transcript(20)
    context.clear_old_tool_results(messages, keep_recent=6)

    assert context.clear_old_tool_results(messages, keep_recent=6) == 0


def test_a_short_loop_is_left_entirely_alone() -> None:
    messages = transcript(4)

    assert context.clear_old_tool_results(messages, keep_recent=6) == 0


def test_only_tool_results_are_cleared() -> None:
    """The user's task and the assistant's reasoning are the thread of the conversation."""
    messages = transcript(20)
    messages.insert(1, {"role": "user", "content": "y" * 5_000})

    context.clear_old_tool_results(messages, keep_recent=1)

    assert messages[1]["content"] == "y" * 5_000


# ---- when it fires --------------------------------------------------------------------------


def test_nothing_happens_until_the_transcript_is_actually_large() -> None:
    """Below the trigger the transcript is not the problem, and clearing would spend a
    cache write for nothing."""
    messages = transcript(5)

    assert context.trim(messages) == 0
    assert set(bodies(messages)) == {"x" * 2_000}


def test_a_large_transcript_is_trimmed() -> None:
    messages = transcript(120)
    before = context.estimate_tokens(messages)

    cleared = context.trim(messages)

    assert before > context.TRIGGER_TOKENS
    assert cleared > 0
    assert context.estimate_tokens(messages) < before


def test_the_estimate_counts_tool_call_arguments_too() -> None:
    """A loop whose calls carry large arguments — a `str_replace` with a file's worth of
    text — is large whatever its results look like."""
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "e", "arguments": "z" * 8_000}}
            ],
        }
    ]

    assert context.estimate_tokens(messages) == 2_000


def test_content_blocks_are_measured_as_well_as_strings() -> None:
    """A moving cache breakpoint turns a tool result into a one-element content list. If
    the estimate could not see through that, trimming would stop firing on exactly the
    long loops it exists for."""
    plain: list[dict[str, Any]] = [{"role": "tool", "content": "x" * 4_000}]
    blocked: list[dict[str, Any]] = [
        {"role": "tool", "content": [{"type": "text", "text": "x" * 4_000}]}
    ]

    assert context.estimate_tokens(plain) == context.estimate_tokens(blocked) == 1_000


# ---- the task budget ---------------------------------------------------------------------


def test_the_roles_that_loop_have_a_ceiling_and_the_others_do_not() -> None:
    """A role that makes one call cannot run away, and a ceiling there would only ever
    fire on a broken request — where the run budget is the better guard."""
    budget = Budget()

    assert budget.task_budget("coder") == 80_000
    assert budget.task_budget("debugger") == 60_000
    assert budget.task_budget("planner") is None


def test_a_step_is_over_when_it_has_spent_its_allowance() -> None:
    req = Request(role="coder", system="s", task_budget_tokens=1_000)

    assert not _over_task_budget(req, Usage(input_tokens=999))
    assert _over_task_budget(req, Usage(input_tokens=1_000))
    assert _over_task_budget(req, Usage(input_tokens=600, output_tokens=600))


def test_a_step_with_no_ceiling_is_never_over() -> None:
    req = Request(role="planner", system="s")

    assert not _over_task_budget(req, Usage(input_tokens=10_000_000))


def test_a_spent_step_is_told_to_land_its_work_not_to_stop() -> None:
    """A Coder forty turns in has usually done most of the work. Discarding it to save the
    last few thousand tokens is the wrong trade — the same reasoning as the run-level
    budget gate, which also asks for a commit rather than an abort."""
    message = _land_it(Request(role="coder", system="s", must_call="submit_result"))

    assert "git_commit" in message
    assert "submit_result" in message
    assert "unfinished" in message
    assert "Do not start anything new" in message


def test_a_step_with_no_required_tool_is_still_asked_to_commit() -> None:
    message = _land_it(Request(role="coder", system="s"))

    assert "git_commit" in message and "submit_result" not in message
