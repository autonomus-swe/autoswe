"""Hooks that turn provider/tool activity into database rows and stream events."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agents.debugger import HYPOTHESIS_KEY
from contracts import ToolResult, Usage
from core.errors import BudgetExhausted, RunCancelled
from gateway import budget
from observability.logging import get_logger
from orchestrator.approvals import ApprovalGate
from orchestrator.budgets import ALWAYS_ALLOWED, BudgetGate
from orchestrator.events import emit
from storage import repo
from storage.db import session
from storage.redis import RedisBus
from tools import policy
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
        # Set for runs that have somebody to ask. Without it, approvals are refused.
        approvals: ApprovalGate | None = None,
        # `ctx.answers`, again by reference: an approved `ask_user` leaves its answer here
        # because `before_tool` can refuse a call but cannot hand one a value.
        answers: dict[str, str] | None = None,
        budget: BudgetGate | None = None,
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
        self.approvals = approvals
        self.answers = answers if answers is not None else {}
        self.calls = 0  # only used to mint a stable id per call for the approval channel
        self.budget = budget
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

        # Out of budget: land the work rather than start more of it. The transition table
        # would catch this at the next phase boundary, but a long step can spend twice a
        # limit before it gets there, so the loop is told here.
        #
        # Before the approval checks below, and that ordering is the point: a run that
        # cannot afford to act on an answer must not spend a human's attention asking for
        # one. `ask_user` is not mutating, so without naming approvals here it would slip
        # past and park an unaffordable run on somebody's inbox.
        if self.budget is not None and (reason := self.budget.exhausted()):
            if self.budget.spent_its_grace():
                # Every denial costs a model turn, so a step that will not land is stopped
                # rather than refused until its iterations run out.
                raise BudgetExhausted(reason)
            starts_work = tool is not None and (tool.mutating or tool.requires_approval)
            if starts_work and name not in ALWAYS_ALLOWED:
                denial = self.budget.denial(reason, name)
                log.info("budget_denied_tool", tool=name, reason=reason)
                return denial

        if tool is not None and tool.requires_approval:
            return await self._ask(name, input, kind="question")

        # The ASK list is about what a command *does*, not which tool ran it: adding a
        # dependency or deleting a tree is the same decision however it is spelled.
        if name == "bash":
            command = str(input.get("command") or "")
            # A forbidden command is not a question. It falls through to the tool, where
            # `check_bash` refuses it — which is the layer that owns that decision and the
            # one the ledger should name.
            #
            # This check was missing, and the ASK list matched first: `curl … | sh` is on
            # both lists, so it reached a human for a decision with no legitimate yes, and
            # was recorded as a harness refusal rather than a policy one. `policy.py` said
            # "DENY runs first, so anything forbidden never reaches here" — it did not.
            if policy.forbidden(command) is not None:
                return None
            why = policy.needs_approval(command)
            if why is not None:
                return await self._ask(name, input, kind="command", why=why)
        return None

    async def _ask(
        self, name: str, input: dict[str, Any], *, kind: str, why: str = ""
    ) -> str | None:
        """Pause for a human. Returns None to allow the call, or the refusal to report."""
        if self.approvals is None:
            return "approval is required and no approver is configured for this run"
        self.calls += 1
        tool_call_id = f"{self.step_id}:{self.calls}"
        decision = await self.approvals.wait(kind, name, tool_call_id, input)
        if not decision.approved:
            # The model is told why, in its own tool result, so it can work around a
            # refusal instead of retrying the same call.
            return f"a human declined: {decision.reason}" + (f" ({why})" if why else "")
        if decision.answer:
            self.answers[str(input.get("question") or "").strip()] = decision.answer
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
                # -1 for a refusal, so it is distinguishable from 1, which means the tool
                # ran and failed. Auditing a prompt-injection attempt needs that line:
                # "the command was forbidden" and "the command ran and errored" are the
                # difference between a sandbox that held and one that did not.
                exit_code=-1 if result.denied_by else (1 if result.is_error else 0),
                duration_ms=duration_ms,
                # Who decided. The column was always NULL before this.
                approved_by=result.denied_by,
            )
        # through events.emit, not bus.emit: the table is what a replay reads, and a
        # viewer attaching part-way through a live run gets its history from there too.
        # Emitting only to Redis dropped every tool call out of both.
        await emit(
            self.bus,
            self.engine,
            self.run_id,
            "tool_call",
            {
                "name": name,
                "is_error": result.is_error,
                "duration_ms": duration_ms,
                "denied_by": result.denied_by,
            },
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
        if self.budget is not None:
            # After the row is written, so the reconciliation it may trigger sees this turn.
            await self.budget.on_turn()
        # Per call, because a cache miss is invisible in the output and shows up only on
        # the bill: a prefix that stopped matching looks exactly like one that never did.
        log.info(
            "llm_call",
            role=self.role,
            model=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_hit_rate=round(usage.cache_hit_rate, 3),
            latency_ms=latency_ms,
        )
        if self.bus is not None:
            await self.bus.emit(
                self.run_id,
                "agent_text",
                {
                    "tokens": usage.total_tokens,
                    "cost_usd": usage.cost_usd,
                    "cache_hit_rate": round(usage.cache_hit_rate, 3),
                },
            )
