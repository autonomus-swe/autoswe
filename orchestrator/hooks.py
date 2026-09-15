"""Hooks that turn provider/tool activity into database rows and stream events."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agents.debugger import HYPOTHESIS_KEY
from contracts import ToolResult, Usage
from core.errors import RunCancelled
from gateway import budget
from observability.logging import get_logger
from orchestrator.events import emit
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
        role: str = "",
        # The agent's own `ctx.submitted`, by reference rather than by copy: the gate has
        # to see a submission the moment the tool records it, mid-loop.
        submitted: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.step_id = step_id
        self.engine = engine
        self.bus = bus
        self.provider_name = provider_name
        self.model = model
        self.effort = effort
        self.role = role
        self.submitted = submitted if submitted is not None else {}
        self.usage = Usage()
        self.tool_calls = 0

    async def before_tool(self, name: str, input: dict[str, Any]) -> str | None:
        # A cancel must land within one tool call, not one node: a coder loop can run for
        # minutes. The provider calls this outside its try/except, so raising stops the
        # loop instead of being turned into another tool error the model would retry.
        if self.bus is not None and await self.bus.is_cancelled(self.run_id):
            raise RunCancelled("cancelled before running " + name)
        tool = REGISTRY.get(name)

        # The Debugger states a hypothesis before it touches anything. A hypothesis
        # written after the edit describes the edit; written before, it is a claim the
        # next test run confirms or refutes, which is the whole value of the step. The
        # gate lives here because this is the only place every tool call passes through.
        if (
            self.role == "debugger"
            and HYPOTHESIS_KEY not in self.submitted
            and tool is not None
            and tool.mutating
        ):
            return (
                f"call submit_hypothesis first — {name} changes things, and a diagnosis "
                "written afterwards is not a diagnosis. Read with read_file and "
                "search_code, then submit your hypothesis."
            )

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
        # through events.emit, not bus.emit: the table is what a replay reads, and a
        # viewer attaching part-way through a live run gets its history from there too.
        # Emitting only to Redis dropped every tool call out of both.
        await emit(
            self.bus,
            self.engine,
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
