"""The Planner: goal plus repository into an implementation plan, with open questions."""

from __future__ import annotations

from typing import ClassVar

from agents.base import Agent
from agents.submit import submit_tool
from contracts import ImplementationPlan, RepoProfile
from core.errors import AgentError
from gateway.provider import Hooks, LLMProvider, RunOutcome
from tools.base import RunContext
from tools.registry import ROLE_TOOLS

SUBMIT_KEY = "plan"
UNATTENDED_NOTE = (
    "No human is available to answer questions on this run. Decide every open point "
    "yourself, state each assumption explicitly in `approach`, and return "
    "`open_questions` as an empty list."
)


def planner_message(
    goal: str,
    profile: RepoProfile,
    repo_map: str,
    answers: list[tuple[str, str]] | None = None,
    unattended_note: str | None = None,
) -> str:
    conventions = "\n".join(f"- {c}" for c in profile.conventions) or "- (none reported)"
    parts = [
        f"# Goal\n{goal}",
        (
            "# Repository profile\n"
            f"languages: {', '.join(profile.languages)}\n"
            f"framework: {profile.framework or 'none'}\n"
            f"package manager: {profile.package_manager}\n"
            f"test command: {profile.test_command}\n"
            f"lint command: {profile.lint_command or 'none'}\n"
            f"entry points: {', '.join(profile.entry_points) or 'none'}\n"
            f"conventions:\n{conventions}"
        ),
        f"# Repository map\n{repo_map}",
    ]
    for question, answer in answers or []:
        parts.append(f"# Answered by the user\nQ: {question}\nA: {answer}")
    if unattended_note:
        parts.append(f"# Constraint\n{unattended_note}")
    parts.append("Read what you need, then call submit_plan once.")
    return "\n\n".join(parts)


class PlannerAgent(Agent):
    role: ClassVar[str] = "planner"
    prompt_file: ClassVar[str] = "planner"
    tool_names: ClassVar[list[str]] = ROLE_TOOLS["planner"]

    async def run(
        self,
        provider: LLMProvider,
        ctx: RunContext,
        goal: str,
        profile: RepoProfile,
        repo_map: str,
        hooks: Hooks,
        answers: list[tuple[str, str]] | None = None,
        unattended: bool = False,
    ) -> tuple[ImplementationPlan, RunOutcome]:
        plan, outcome = await self._once(provider, ctx, goal, profile, repo_map, hooks, answers)
        if plan.open_questions and unattended:
            # nobody can answer, so ask the planner to decide and record its assumptions
            ctx.submitted.pop(SUBMIT_KEY, None)
            plan, outcome = await self._once(
                provider, ctx, goal, profile, repo_map, hooks, answers, UNATTENDED_NOTE
            )
            plan = plan.model_copy(update={"open_questions": []})
        return plan, outcome

    async def _once(
        self,
        provider: LLMProvider,
        ctx: RunContext,
        goal: str,
        profile: RepoProfile,
        repo_map: str,
        hooks: Hooks,
        answers: list[tuple[str, str]] | None = None,
        note: str | None = None,
    ) -> tuple[ImplementationPlan, RunOutcome]:
        submit = submit_tool("submit_plan", ImplementationPlan, SUBMIT_KEY)
        outcome = await self.run_tools(
            provider,
            ctx,
            planner_message(goal, profile, repo_map, answers, note),
            hooks,
            extra_tools=[submit],
            must_call=submit.name,
        )
        plan = ctx.submitted.get(SUBMIT_KEY)
        if not isinstance(plan, ImplementationPlan):
            raise AgentError(
                f"planner did not submit a plan (stop_reason={outcome.stop_reason}, "
                f"turns={outcome.turns})"
            )
        return plan, outcome
