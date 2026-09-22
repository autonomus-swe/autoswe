"""The Debugger: a hypothesis first, then a fix.

The order matters more than anything else here. A hypothesis submitted after the edit is
a description of the edit; submitted before, it is a claim that the next test run either
confirms or refutes. The gate that enforces it lives in ``OrchestratorHooks.before_tool``
— it has to, because that is the only place every tool call passes through.
"""

from __future__ import annotations

from typing import ClassVar

from agents.base import Agent, fence
from agents.submit import submit_tool
from contracts import DebugHypothesis, TaskResult, TaskSpec, TestReport
from core.errors import AgentError
from gateway.provider import Hooks, LLMProvider, RunOutcome
from tools.base import RunContext
from tools.registry import ROLE_TOOLS

HYPOTHESIS_KEY = "hypothesis"
RESULT_KEY = "task_result"
MAX_FAILURES_SHOWN = 6
MAX_CONTEXT_FRAMES = 3


def render_report(report: TestReport, context: dict[str, str] | None = None) -> str:
    """The failing tests as evidence: what failed, how, and the code at each frame.

    Source context is fenced as untrusted, the same as any other repository content. A
    comment in a test file is not an instruction.
    """
    lines = [
        f"{report.failed} failed, {report.errors} errors of {report.total} (`{report.command}`)"
    ]
    for fail in report.failures[:MAX_FAILURES_SHOWN]:
        lines.append(f"\n## {fail.test_id}\nkind: `{fail.kind}`\nmessage: {fail.message}")
        shown = 0
        for frame in fail.frames:
            if not frame.in_repo or shown >= MAX_CONTEXT_FRAMES:
                continue
            shown += 1
            lines.append(f"\n{frame.file}:{frame.line} in `{frame.function}`")
            around = (context or {}).get(f"{fail.test_id}|{frame.file}:{frame.line}")
            lines.append(fence(f"{frame.file} around line {frame.line}", around or frame.code))
        if not shown:
            lines.append("\n(no frames inside the repository — see the message and output)")
    if len(report.failures) > MAX_FAILURES_SHOWN:
        lines.append(f"\n… and {len(report.failures) - MAX_FAILURES_SHOWN} more failures")
    return "\n".join(lines)


def render_attempts(previous: list[DebugHypothesis]) -> str:
    """What earlier attempts believed, so attempt three does not repeat attempt one."""
    if not previous:
        return ""
    out = ["# Previous attempts on this task"]
    for i, h in enumerate(previous, 1):
        out.append(
            f"\n## Attempt {i} (confidence {h.confidence:.2f}, class `{h.failure_class}`)\n"
            f"believed: {h.root_cause}\nplanned: {h.plan}\noutcome: the tests still failed."
        )
    return "\n".join(out)


ALTERNATIVE_BLOCK = """\
# The last attempt changed nothing

The tests failed with exactly the same signature as before your previous attempt: same
test, same kind of failure, same function. Whatever you changed did not affect the cause.

So that class of fix is off the table. Your new hypothesis must say what was wrong with
the previous one — not restate it in different words — and then look somewhere you have
not looked yet. If you are now unsure the cause is where you thought, say so.\
"""


def debug_message(
    goal: str,
    task: TaskSpec,
    report: TestReport,
    *,
    previous: list[DebugHypothesis] | None = None,
    alternative: bool = False,
    human_hint: str | None = None,
    context: dict[str, str] | None = None,
) -> str:
    criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria) or "- (none given)"
    parts = [
        f"# Goal\n{goal}",
        f"# Task {task.id}: {task.title}\n{task.description}",
        f"# Acceptance criteria\n{criteria}",
        f"# Failing tests\n{render_report(report, context)}",
    ]
    if previous:
        parts.append(render_attempts(previous))
    if alternative:
        parts.append(ALTERNATIVE_BLOCK)
    if human_hint:
        parts.append(f"# A human left this note\n{human_hint}")
    parts.append("Read enough to be sure, call submit_hypothesis, then fix it and submit_result.")
    return "\n\n".join(parts)


class DebuggerAgent(Agent):
    role: ClassVar[str] = "debugger"
    prompt_file: ClassVar[str] = "debugger"
    tool_names: ClassVar[list[str]] = ROLE_TOOLS["debugger"]

    async def run(
        self,
        provider: LLMProvider,
        ctx: RunContext,
        goal: str,
        task: TaskSpec,
        report: TestReport,
        hooks: Hooks,
        *,
        previous: list[DebugHypothesis] | None = None,
        alternative: bool = False,
        human_hint: str | None = None,
        context: dict[str, str] | None = None,
    ) -> tuple[DebugHypothesis, TaskResult | None, RunOutcome]:
        hypothesis_tool = submit_tool("submit_hypothesis", DebugHypothesis, HYPOTHESIS_KEY)
        result_tool = submit_tool("submit_result", TaskResult, RESULT_KEY)
        outcome = await self.run_tools(
            provider,
            ctx,
            debug_message(
                goal,
                task,
                report,
                previous=previous,
                alternative=alternative,
                human_hint=human_hint,
                context=context,
            ),
            hooks,
            extra_tools=[hypothesis_tool, result_tool],
            # The hypothesis, not the result — `must_call` takes one tool and this step
            # has two, so it has to be the one whose absence is fatal. Twenty lines below,
            # a missing hypothesis raises and a missing result is explicitly survivable;
            # enforcing the result therefore guarded the outcome the step can live without
            # and left the one that kills it to chance.
            #
            # Measured on a Django scale run: the loop forced `submit_result` twice,
            # succeeded both times, and the step still died with "debugger did not submit
            # a hypothesis". Every reminder and the forced call were spent on the wrong
            # tool.
            must_call=hypothesis_tool.name,
        )
        hypothesis = ctx.submitted.get(HYPOTHESIS_KEY)
        if not isinstance(hypothesis, DebugHypothesis):
            # Without a hypothesis there is nothing to record and nothing to have been
            # tested, so this is a failed attempt rather than a partial one.
            raise AgentError(
                f"debugger did not submit a hypothesis (stop_reason={outcome.stop_reason}, "
                f"turns={outcome.turns})"
            )
        # A missing result is survivable: the hypothesis is still worth recording, and the
        # next TEST decides whether the edits helped.
        result = ctx.submitted.get(RESULT_KEY)
        return hypothesis, result if isinstance(result, TaskResult) else None, outcome
