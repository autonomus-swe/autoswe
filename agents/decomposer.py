"""The Decomposer: a plan into a validated task DAG. No tools, pure structured output."""

from __future__ import annotations

from typing import ClassVar

from agents.base import Agent
from contracts import (
    DebugHypothesis,
    ImplementationPlan,
    RepoProfile,
    TaskGraph,
    TaskGraphSpec,
    TaskSpec,
    TestReport,
)
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


def replan_message(
    goal: str,
    task: TaskSpec,
    report: TestReport | None,
    hypotheses: list[DebugHypothesis],
) -> str:
    """Ask for a different shape of work, given everything that has already been tried."""
    tried = (
        "\n".join(
            f"- attempt {i}: believed {h.root_cause} — planned {h.plan} — did not work"
            for i, h in enumerate(hypotheses, 1)
        )
        or "- (no hypotheses were recorded)"
    )
    failing = (
        "\n".join(f"- {f.test_id} [{f.kind}] {f.message}" for f in report.failures[:8])
        if report
        else "- (no report)"
    )
    return "\n\n".join(
        [
            f"# Goal\n{goal}",
            (
                f"# The task that failed\n{task.id}: {task.title}\n{task.description}\n"
                "acceptance criteria:\n"
                + ("\n".join(f"- {c}" for c in task.acceptance_criteria) or "- (none)")
            ),
            f"# Still failing\n{failing}",
            f"# What was already tried\n{tried}",
            (
                "# What to return\n"
                "Replace this one task with one to three smaller tasks, or the same work "
                "approached differently. Between them they must still satisfy every "
                "acceptance criterion above — you are re-cutting the work, not reducing "
                "it. Do not propose changing or deleting tests. If the attempts above "
                "suggest the original approach cannot work, say so in a task description "
                "and propose the alternative."
            ),
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

    async def replan(
        self,
        provider: LLMProvider,
        goal: str,
        task: TaskSpec,
        report: TestReport | None,
        hypotheses: list[DebugHypothesis],
    ) -> TaskGraphSpec | None:
        """Re-cut one exhausted task. Returns None rather than raising.

        A failed replan is not a crash: the escalation path still has a human to ask, and
        losing the run because the re-cut came back malformed would throw away the
        remaining option.
        """
        message = replan_message(goal, task, report, hypotheses)
        try:
            spec = await self.run_structured(provider, message, TaskGraphSpec)
        except Exception as e:
            log.warning("replan_unusable", task_id=task.id, error=f"{type(e).__name__}: {e}")
            return None
        if not spec.tasks:
            return None
        # Ids must not collide with the graph this is being spliced into.
        for i, t in enumerate(spec.tasks, 1):
            t.id = f"{task.id}.{i}"
            t.depends_on = [d for d in t.depends_on if d.startswith(f"{task.id}.")]
        return spec
