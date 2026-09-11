"""The Coder: one task, typed tools, ends with ``submit_result``."""

from __future__ import annotations

from typing import ClassVar

from agents.base import Agent, fence
from agents.submit import submit_tool
from contracts import TaskResult, TaskSpec
from core.errors import AgentError
from gateway.provider import Hooks, LLMProvider, RunOutcome
from tools.base import RunContext
from tools.registry import ROLE_TOOLS

SUBMIT_KEY = "task_result"


def task_message(goal: str, task: TaskSpec, files: dict[str, str] | None = None) -> str:
    criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria) or "- (none given)"
    parts = [
        f"# Goal\n{goal}",
        f"# Task {task.id}: {task.title}\n{task.description}",
        f"# Acceptance criteria\n{criteria}",
        f"# Test selector\n`{task.test_selector or '(full suite)'}`",
    ]
    if task.files:
        parts.append("# Files to look at first\n" + "\n".join(f"- {f}" for f in task.files))
    for path, content in (files or {}).items():
        parts.append(fence(path, content))
    parts.append("Start by viewing the repository, then implement the task and submit_result.")
    return "\n\n".join(parts)


class CoderAgent(Agent):
    role: ClassVar[str] = "coder"
    prompt_file: ClassVar[str] = "coder"
    tool_names: ClassVar[list[str]] = ROLE_TOOLS["coder"]

    async def run(
        self,
        provider: LLMProvider,
        ctx: RunContext,
        goal: str,
        task: TaskSpec,
        hooks: Hooks,
        files: dict[str, str] | None = None,
    ) -> tuple[TaskResult, RunOutcome]:
        submit = submit_tool("submit_result", TaskResult, SUBMIT_KEY)
        outcome = await self.run_tools(
            provider,
            ctx,
            task_message(goal, task, files),
            hooks,
            extra_tools=[submit],
            must_call=submit.name,
            test_command=ctx.test_command,
        )
        result = ctx.submitted.get(SUBMIT_KEY)
        if not isinstance(result, TaskResult):
            raise AgentError(
                f"coder did not submit a result (stop_reason={outcome.stop_reason}, "
                f"turns={outcome.turns})"
            )
        return result, outcome
