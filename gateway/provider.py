"""Provider-agnostic request/response shapes. Agents depend on this, never on an SDK."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from contracts import ToolResult, Usage
from tools.base import BaseTool, RunContext


@dataclass
class Request:
    role: str  # ROUTES key
    system: str  # the role prompt: static per role, and kept that way — see gateway/caching
    # Repository facts, conventions and map: fixed for a whole run, so it sits in the
    # cached prefix rather than in the messages, where it would be re-sent uncached on
    # every call of every step.
    run_block: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)  # chat messages after system
    max_tokens: int = 16_000
    max_iterations: int = 60
    # A tool the agent must call before its turn may end (e.g. "submit_result"). When the
    # model stops without it, the loop reminds it rather than discarding the whole run.
    must_call: str | None = None


@dataclass
class RunOutcome:
    final_text: str
    turns: int
    usage: Usage
    stop_reason: str  # end_turn | max_tokens | max_iterations | refusal


class Hooks(Protocol):
    async def before_tool(self, name: str, input: dict[str, Any]) -> str | None: ...

    async def after_tool(
        self, name: str, input: dict[str, Any], result: ToolResult, duration_ms: int
    ) -> ToolResult: ...

    async def on_message(self, message: Any, usage: Usage, latency_ms: int) -> None: ...


class NullHooks:
    async def before_tool(self, name: str, input: dict[str, Any]) -> str | None:
        return None

    async def after_tool(
        self, name: str, input: dict[str, Any], result: ToolResult, duration_ms: int
    ) -> ToolResult:
        return result

    async def on_message(self, message: Any, usage: Usage, latency_ms: int) -> None:
        return None


class LLMProvider(Protocol):
    provider_name: str
    model: str

    async def parse[T: BaseModel](self, req: Request, output: type[T]) -> tuple[T, Usage]: ...

    async def run_tools(
        self, req: Request, tools: Sequence[BaseTool], ctx: RunContext, hooks: Hooks
    ) -> RunOutcome: ...
