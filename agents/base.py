"""Agent base: prompt loading/rendering and the two ways an agent runs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from contracts import Usage
from gateway.provider import Hooks, LLMProvider, Request, RunOutcome
from gateway.routing import ROUTES
from observability.tracing import annotate, trace_span
from tools.base import BaseTool, RunContext
from tools.registry import REGISTRY

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _usage_attrs(usage: Usage) -> dict[str, Any]:
    """What a step cost, for the span. The same numbers the ledger holds, where somebody
    reading a trace can see them beside the time they took."""
    return {
        "step_input_tokens": usage.input_tokens,
        "step_output_tokens": usage.output_tokens,
        "step_cache_read_tokens": usage.cache_read_tokens,
        "step_cache_hit_rate": round(usage.cache_hit_rate, 3),
        "step_cost_usd": usage.cost_usd,
    }


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
        # The step span goes here rather than at the eleven call sites in `nodes.py`: this
        # is what every one of them funnels through, and putting it here makes `llm_call`
        # and `tool.*` nest inside it without anyone having to remember to.
        with self._step_span(provider):
            outcome = await provider.run_tools(
                req, [*self.tools(), *(extra_tools or [])], ctx, hooks
            )
            # Inside the block, not after it: `annotate` writes to whatever span is
            # current, and one line further down that is the *phase*, where a step's token
            # count reads as the phase's.
            annotate(
                step_turns=outcome.turns,
                step_stop_reason=outcome.stop_reason,
                **_usage_attrs(outcome.usage),
            )
        return outcome

    def _step_span(self, provider: LLMProvider) -> Any:
        """`step.<role>`, named with what was actually routed.

        Effort comes from `ROUTES` rather than from the caller: a downgrade changes the
        tier and deliberately leaves the effort alone, so the table is always right about
        it and there is nothing to thread through.
        """
        return trace_span(
            f"step.{self.role}",
            role=self.role,
            model=provider.model_for(self.tier),
            tier=self.tier,
            effort=ROUTES[self.role].effort if self.role in ROUTES else None,
        )

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
        with self._step_span(provider):
            obj, usage = await provider.parse(req, output)
            annotate(step_structured=output.__name__, **_usage_attrs(usage))
        if hooks is not None:
            await hooks.on_message(obj, usage, int((time.monotonic() - started) * 1000))
        return obj
