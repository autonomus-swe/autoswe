"""Pausing a run mid-tool-call to ask a human, and resuming with their answer.

This is a different shape of interrupt from ``AWAITING_INPUT``. An open question happens
between nodes, so the run's *phase* can be AWAITING_INPUT. An approval happens inside an
agent's tool loop, with a half-finished turn on the stack — so the run's **status** is
``awaiting_input`` while its ``phase`` stays CODE or DEBUG. Both are visible on
``GET /runs/{id}``, and the distinction is why this cannot reuse the node.

The pending call id lives in Redis rather than on the state, because the API process has
to validate an approval against it and never sees the run state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from observability.logging import get_logger
from orchestrator.events import emit
from storage import repo as db
from storage.db import session
from storage.redis import RedisBus

log = get_logger(__name__)

POLL_S = 5
DEFAULT_TIMEOUT_S = 4 * 3600
UNATTENDED_REJECTION = "unattended run: not permitted"
TIMEOUT_REJECTION = "no response from a human in time"


@dataclass(frozen=True)
class Decision:
    """What the human said. ``answer`` carries text for ``ask_user``."""

    approved: bool
    reason: str = ""
    answer: str = ""


@dataclass
class ApprovalGate:
    """Blocks one tool call until a human decides. Holds the run's locks while it waits."""

    run_id: UUID
    engine: Any
    bus: RedisBus | None
    unattended: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S
    # Called with seconds waited, so the run can keep that out of its budget.
    on_wait: Any = None
    # Called to renew the repo lock while parked; a long wait must not drop it.
    renew: Any = None

    async def wait(
        self, kind: str, tool_name: str, tool_call_id: str, input: dict[str, Any]
    ) -> Decision:
        if self.unattended:
            # Nobody is listening, so refuse rather than hang. The model is told why and
            # can work around it, which is more useful than a run that never finishes.
            log.info("approval_auto_rejected", tool=tool_name, kind=kind)
            return Decision(approved=False, reason=UNATTENDED_REJECTION)
        if self.bus is None:  # no bus, no way to ask
            return Decision(approved=False, reason="no approval channel is configured")

        await self.bus.set_pending(self.run_id, tool_call_id)
        async with session(self.engine) as s:
            await db.set_run_status(s, self.run_id, "awaiting_input")
        await emit(
            self.bus,
            self.engine,
            self.run_id,
            "awaiting_input",
            {
                "kind": kind,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "input": input,
            },
        )
        log.info("approval_requested", tool=tool_name, kind=kind, tool_call_id=tool_call_id)

        started = time.monotonic()
        try:
            return await self._listen(tool_call_id, tool_name, started)
        finally:
            waited = time.monotonic() - started
            if self.on_wait is not None:
                self.on_wait(waited)
            await self.bus.clear_pending(self.run_id)
            async with session(self.engine) as s:
                await db.set_run_status(s, self.run_id, "running")

    async def _listen(self, tool_call_id: str, tool_name: str, started: float) -> Decision:
        assert self.bus is not None
        while True:
            message = await self.bus.pop_inbox(self.run_id, timeout_s=POLL_S)
            if message:
                decision = self._read(message, tool_call_id)
                if decision is not None:
                    log.info(
                        "approval_decided",
                        tool=tool_name,
                        approved=decision.approved,
                        reason=decision.reason[:120],
                    )
                    return decision
                # A message for a different call, or a kind we do not handle. Put it back
                # rather than swallow it: it may be an answer some other waiter needs.
                await self.bus.push_inbox(self.run_id, message)
            if await self.bus.is_cancelled(self.run_id):
                return Decision(approved=False, reason="the run was cancelled")
            if self.renew is not None:
                await self.renew()
            if time.monotonic() - started > self.timeout_s:
                return Decision(approved=False, reason=TIMEOUT_REJECTION)

    @staticmethod
    def _read(message: dict[str, Any], tool_call_id: str) -> Decision | None:
        """A decision for *this* call, or None when the message is not one."""
        kind = message.get("type")
        if kind not in ("approve", "reject", "answer"):
            return None
        target = message.get("tool_call_id")
        # An `answer` with no id is for the Planner's open questions, not for a tool.
        if target != tool_call_id:
            return None
        if kind == "approve":
            return Decision(approved=True)
        if kind == "reject":
            return Decision(approved=False, reason=str(message.get("reason") or "rejected"))
        return Decision(approved=True, answer=str(message.get("text") or ""))
