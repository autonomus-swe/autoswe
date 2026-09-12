"""Hooks that turn provider/tool activity into database rows and stream events."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from contracts import ToolResult, Usage
from core.errors import RunCancelled
from gateway import budget
from observability.logging import get_logger
from storage import repo
from storage.db import session
from storage.redis import RedisBus
from tools.registry import REGISTRY

log = get_logger(__name__)
PREVIEW_CHARS = 2000


class OrchestratorHooks:
    """Audit every tool call and every model turn. Phase 3 adds approval gates here."""

    def __init__(
        self,
        *,
        run_id: UUID,
        step_id: UUID,
        engine: Any,
        bus: RedisBus | None,
        provider_name: str,
        model: str,
        effort: str | None,
    ) -> None:
        self.run_id = run_id
        self.step_id = step_id
        self.engine = engine
        self.bus = bus
        self.provider_name = provider_name
        self.model = model
        self.effort = effort
        self.usage = Usage()
        self.tool_calls = 0

    async def before_tool(self, name: str, input: dict[str, Any]) -> str | None:
        # A cancel must land within one tool call, not one node: a coder loop can run for
        # minutes. The provider calls this outside its try/except, so raising stops the
        # loop instead of being turned into another tool error the model would retry.
        if self.bus is not None and await self.bus.is_cancelled(self.run_id):
            raise RunCancelled("cancelled before running " + name)
        tool = REGISTRY.get(name)
        if tool is not None and tool.requires_approval:
            return "approval is required but no approver is configured in this phase"
        return None

    async def after_tool(
        self, name: str, input: dict[str, Any], result: ToolResult, duration_ms: int
    ) -> ToolResult:
        self.tool_calls += 1
        async with session(self.engine) as s:
            await repo.insert_tool_call(
                s,
                step_id=self.step_id,
                name=name,
                input=input,
                output_preview=result.content[:PREVIEW_CHARS],
                exit_code=1 if result.is_error else 0,
                duration_ms=duration_ms,
            )
        if self.bus is not None:
            await self.bus.emit(
                self.run_id,
                "tool_call",
                {"name": name, "is_error": result.is_error, "duration_ms": duration_ms},
            )
        return result

    async def on_message(self, message: Any, usage: Usage, latency_ms: int) -> None:
        self.usage = self.usage.add(usage)
        async with session(self.engine) as s:
            await budget.record_llm_call(
                s,
                step_id=self.step_id,
                provider=self.provider_name,
                model=self.model,
                effort=self.effort,
                usage=usage,
                latency_ms=latency_ms,
                stop_reason=getattr(message, "finish_reason", None),
            )
        if self.bus is not None:
            await self.bus.emit(
                self.run_id,
                "agent_text",
                {"tokens": usage.total_tokens, "cost_usd": usage.cost_usd},
            )
