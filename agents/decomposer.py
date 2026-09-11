"""The Decomposer: a plan into a validated task DAG. No tools, pure structured output."""

from __future__ import annotations

from typing import ClassVar

from agents.base import Agent
from contracts import ImplementationPlan, RepoProfile, TaskGraph, TaskGraphSpec
from core.errors import AgentError
from gateway.provider import LLMProvider
from observability.logging import get_logger

log = get_logger(__name__)


def decomposer_message(
    goal: str, plan: ImplementationPlan, profile: RepoProfile, repo_map: str
) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) or "- (none)"

    return "\n\n".join(
        [
            f"# Goal\n{goal}",
            (
                "# Plan\n"
                f"approach: {plan.approach}\n"
                f"affected files:\n{bullets(plan.affected_files)}\n"
                f"new files:\n{bullets(plan.new_files)}\n"
                f"risks:\n{bullets(plan.risks)}\n"
                f"test strategy: {plan.test_strategy}"
            ),
            f"# Test command\n{profile.test_command}",
            f"# Repository map\n{repo_map}",
            "Return the task graph.",
        ]
    )


class DecomposerAgent(Agent):
    role: ClassVar[str] = "decomposer"
    prompt_file: ClassVar[str] = "decomposer"
    tool_names: ClassVar[list[str]] = []

    async def run(
        self,
        provider: LLMProvider,
        goal: str,
        plan: ImplementationPlan,
        profile: RepoProfile,
        repo_map: str,
    ) -> TaskGraph:
        message = decomposer_message(goal, plan, profile, repo_map)
        spec = await self.run_structured(provider, message, TaskGraphSpec)
        graph = TaskGraph.from_spec(spec)
        problems = graph.validate_dag()
        if not problems:
            return graph

        # one correction round: the model is told exactly what is wrong with its own graph
        log.warning("task_graph_invalid", problems=problems)
        retry = (
            f"{message}\n\n# Your previous graph was invalid\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nFix these and return the complete graph again."
        )
        graph = TaskGraph.from_spec(await self.run_structured(provider, retry, TaskGraphSpec))
        problems = graph.validate_dag()
        if problems:
            raise AgentError("decomposer produced an invalid task graph: " + "; ".join(problems))
        return graph
