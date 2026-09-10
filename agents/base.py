"""Agent base: prompt loading/rendering and the two ways an agent runs."""

from __future__ import annotations

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
            messages=[{"role": "user", "content": user_content}],
            max_iterations=self.max_iterations,
            must_call=must_call,
        )
        return await provider.run_tools(req, [*self.tools(), *(extra_tools or [])], ctx, hooks)

    async def run_structured[T: BaseModel](
        self, provider: LLMProvider, user_content: str, output: type[T], **prompt_vars: Any
    ) -> T:
        req = Request(
            role=self.role,
            system=self.system_prompt(**prompt_vars),
            messages=[{"role": "user", "content": user_content}],
        )
        obj, _usage = await provider.parse(req, output)
        return obj
