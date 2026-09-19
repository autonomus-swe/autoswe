"""Agent base: prompt loading/rendering and the two ways an agent runs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from gateway.provider import Hooks, LLMProvider, Request, RunOutcome
from tools.base import BaseTool, RunContext
from tools.registry import REGISTRY

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def fence(path: str, content: str) -> str:
    """Wrap repository content in the untrusted-content fence used by every prompt."""
    return load_prompt("_fences").format(path=path, content=content)


class Agent:
    role: ClassVar[str]
    prompt_file: ClassVar[str]
    tool_names: ClassVar[list[str]] = []
    max_iterations: ClassVar[int] = 60

    def __init__(self, run_block: str | None = None, tier: str | None = None) -> None:
        """Per-run context and the model tier this step was routed to.

        Held on the agent rather than threaded through every ``run()`` signature: each
        agent takes different arguments, and a parameter that all eleven of them accept
        and none of them read is a parameter that gets dropped by the twelfth. Both reach
        the request in one place — the two methods below — so neither can be forgotten.

        ``tier`` defaults to ``None``, which the provider resolves to its own model. An
        agent constructed without one therefore behaves exactly as it did before routing
        could change it.
        """
        self.run_block = run_block
        self.tier = tier

    def system_prompt(self, **vars: Any) -> str:
        template = load_prompt(self.prompt_file)
        # str.format with escaped braces: literal {{ }} in prompts survive rendering
        return template.format(**vars)

    def tools(self) -> list[BaseTool]:
        return [REGISTRY[name] for name in self.tool_names]

    async def run_tools(
        self,
        provider: LLMProvider,
        ctx: RunContext,
        user_content: str,
        hooks: Hooks,
        *,
        extra_tools: list[BaseTool] | None = None,
        must_call: str | None = None,
        **prompt_vars: Any,
    ) -> RunOutcome:
        req = Request(
            role=self.role,
            system=self.system_prompt(**prompt_vars),
            run_block=self.run_block,
            tier=self.tier,
            messages=[{"role": "user", "content": user_content}],
            max_iterations=self.max_iterations,
            must_call=must_call,
        )
        return await provider.run_tools(req, [*self.tools(), *(extra_tools or [])], ctx, hooks)

    async def run_structured[T: BaseModel](
        self,
        provider: LLMProvider,
        user_content: str,
        output: type[T],
        *,
        hooks: Hooks | None = None,
        max_tokens: int | None = None,
        **prompt_vars: Any,
    ) -> T:
        """One model call, no tools.

        ``hooks`` is how the call reaches the ledger. Budgets are enforced from
        ``llm_calls``, so a structured call that skips the hooks is spend the run cannot
        see — and once that number is authoritative, invisible spend is worse than
        unenforced spend. Callers with a step to attach it to should always pass them.
        """
        req = Request(
            role=self.role,
            system=self.system_prompt(**prompt_vars),
            run_block=self.run_block,
            tier=self.tier,
            messages=[{"role": "user", "content": user_content}],
        )
        if max_tokens:
            req.max_tokens = max_tokens
        started = time.monotonic()
        obj, usage = await provider.parse(req, output)
        if hooks is not None:
            await hooks.on_message(obj, usage, int((time.monotonic() - started) * 1000))
        return obj
