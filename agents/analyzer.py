"""The Analyzer: survey the repository, confirm what detection found, report conventions."""

from __future__ import annotations

from typing import ClassVar

from agents.base import Agent, fence
from agents.submit import submit_tool
from contracts import RepoFacts, RepoProfile
from core.errors import AgentError
from gateway.provider import Hooks, LLMProvider, RunOutcome
from tools.base import RunContext
from tools.registry import ROLE_TOOLS

SUBMIT_KEY = "profile"
# Twelve turns to confirm what the map already says, not to read the repository. The repo
# map arrives ranked and summarised, so the Analyzer's job is to check facts and fill gaps
# — an Analyzer that explores until satisfied on a large tree would spend the run's budget
# before the Coder started.
#
# Measured consequence, from the Django scale run: on 3 043 indexed files this cap is
# reached every time, so the forced-submit path in `run_tools` is the *normal* exit at
# scale rather than an exception. That is the intended trade and not a bug — but it does
# mean the profile for a large repository is assembled from twelve turns of spot checks,
# and whether more turns would produce a better one is unmeasured.
MAX_ITERATIONS = 12


def analyzer_message(goal: str, facts: RepoFacts, repo_map: str) -> str:
    parts = [
        f"# Goal this repository is about to be changed for\n{goal}",
        f"# Detected facts (confirm or correct these)\n{facts.render()}",
        f"# Repository map\n{repo_map}",
    ]
    if facts.readme_head:
        parts.append(fence("README", facts.readme_head))
    parts.append("Survey the repository, then call submit_profile once.")
    return "\n\n".join(parts)


class AnalyzerAgent(Agent):
    role: ClassVar[str] = "analyzer"
    prompt_file: ClassVar[str] = "analyzer"
    tool_names: ClassVar[list[str]] = ROLE_TOOLS["analyzer"]
    max_iterations: ClassVar[int] = MAX_ITERATIONS

    async def run(
        self,
        provider: LLMProvider,
        ctx: RunContext,
        goal: str,
        facts: RepoFacts,
        repo_map: str,
        hooks: Hooks,
    ) -> tuple[RepoProfile, RunOutcome]:
        submit = submit_tool("submit_profile", RepoProfile, SUBMIT_KEY)
        outcome = await self.run_tools(
            provider,
            ctx,
            analyzer_message(goal, facts, repo_map),
            hooks,
            extra_tools=[submit],
            must_call=submit.name,
        )
        profile = ctx.submitted.get(SUBMIT_KEY)
        if not isinstance(profile, RepoProfile):
            raise AgentError(
                f"analyzer did not submit a profile (stop_reason={outcome.stop_reason}, "
                f"turns={outcome.turns})"
            )
        if not profile.test_command.strip():
            raise AgentError("analyzer returned an empty test command")
        return profile, outcome
