"""The control plane, once, for every transport that exposes it.

Phase 6 puts an MCP server in front of the same operations the HTTP routes already offer,
and its plan says the MCP layer holds no logic. That is only possible if the logic lives
somewhere both can call, so it lives here.

The alternative — an MCP tool that opens its own session, checks its own preconditions and
pushes its own inbox message — is a second implementation of a path that already exists.
Phase 5 produced four defects in one twenty-line block written exactly that way, each of
them a case the original handled and the copy forgot. The approval path makes the cost
concrete: `_pending` exists because "an approval could be replayed later against a
different call, which is how a human ends up authorising something they never saw", and a
copy that omitted it would be a security hole reachable from an editor.

## Errors are transport-neutral

These raise `NotFound` and `Conflict` rather than `HTTPException`. A status code is an
answer to "how should this be reported over HTTP", which is the transport's question, not
the control plane's — and an MCP tool raising `HTTPException` would be absurd. Each
transport maps them: FastAPI to 404/409, MCP to its own error shape.

## Dependencies are passed, not fetched

The routes read `request.app.state`. Nothing here does, because a stdio MCP process has no
request and no app. `ControlPlane` is built once by whoever owns the connections and holds
only what these operations need.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from contracts import Budget
from observability.logging import get_logger
from storage import repo as db
from storage.db import session

log = get_logger(__name__)

MAX_ROWS = 200
POLL_S = 10.0
TERMINAL = frozenset({"done", "failed", "cancelled"})
# What `wait_for_run` stops on. `awaiting_input` is in here because a run parked on a
# question is waiting for the caller, and a wait that slept through it would be each side
# waiting for the other.
SETTLED = TERMINAL | {"awaiting_input"}


class ControlError(Exception):
    """Something the caller did wrong, in terms the caller can act on."""


class NotFound(ControlError):
    """No run with that id."""


class Conflict(ControlError):
    """The run is real but not in a state where this makes sense."""


def plane_from(state: Any) -> ControlPlane:
    """A control plane from whatever the app's lifespan opened.

    Takes the state object rather than a request, so this module stays importable without
    FastAPI — the stdio MCP entrypoint has neither a request nor an app.
    """
    return ControlPlane(engine=state.engine, bus=state.bus, arq=getattr(state, "arq", None))


@dataclass(frozen=True)
class ControlPlane:
    """What the control plane needs to do its work, handed to it rather than looked up.

    `arq` is optional and nullable on purpose: a run whose job cannot be enqueued stays
    queued in the database rather than vanishing, which is the existing behaviour and the
    reason the route logged a warning instead of raising.
    """

    engine: Any
    bus: Any
    arq: Any = None

    # ---- runs ---------------------------------------------------------------------

    async def create_run(
        self,
        *,
        repo_url: str,
        goal: str,
        base_branch: str,
        provider: str,
        budget: Budget | None = None,
        unattended: bool = False,
        upstream: str | None = None,
    ) -> uuid.UUID:
        async with session(self.engine) as s:
            run_id = await db.create_run(
                s,
                repo_url=repo_url,
                base_branch=base_branch,
                goal=goal,
                budget=budget or Budget(),
                provider=provider,
                unattended=unattended,
                upstream=upstream,
            )
        if self.arq is not None:
            await self.arq.enqueue_job("run_job", str(run_id), _job_id=str(run_id))
        else:
            # The queue is unavailable: the run stays queued rather than silently
            # vanishing, and something that reads the table can still pick it up.
            log.warning("arq_unavailable", run_id=str(run_id))
        log.info("run_created", run_id=str(run_id), repo_url=repo_url, unattended=unattended)
        return run_id

    async def get_run(self, run_id: uuid.UUID) -> Any:
        """The run row, or `NotFound`. Callers that want a soft miss can catch it."""
        async with session(self.engine) as s:
            row = await db.get_run(s, run_id)
        if row is None:
            raise NotFound("run not found")
        return row

    async def list_runs(self, limit: int = 50) -> list[Any]:
        """Most recent runs first. The limit is clamped here rather than at each transport,
        because "how much is it reasonable to return" is the same question over MCP as
        over HTTP and a caller that asks for a million rows is not owed them."""
        async with session(self.engine) as s:
            return await db.list_runs(s, limit=min(max(limit, 1), MAX_ROWS))

    async def wait_for_run(
        self, run_id: uuid.UUID, *, timeout_s: float = 1800.0, poll_s: float = POLL_S
    ) -> Any:
        """Block until the run either finishes or needs something from you, then return it.

        That means terminal *or* `awaiting_input`: a caller that waits only for terminal
        deadlocks against a run parked on a question it is the one expected to answer.

        On timeout this returns the row as it stands rather than raising. The status says
        which happened — anything non-terminal means the clock ran out — and a partial
        answer with a timestamp is more use to the caller than an exception that discards
        what the run had reached.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            row = await self.get_run(run_id)
            if row.status in SETTLED:
                return row
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.info("wait_timed_out", run_id=str(run_id), status=row.status)
                return row
            await asyncio.sleep(min(poll_s, remaining))

    async def pending_question(self, run_id: uuid.UUID) -> dict[str, Any] | None:
        """What a parked run is waiting for, or None if it is not parked.

        The payload is the `awaiting_input` event's, unchanged: either the Planner's open
        questions or the tool call needing a decision. Both shapes carry the `kind` that
        tells them apart, so this does not flatten them into one.
        """
        async with session(self.engine) as s:
            row = await db.latest_event(s, run_id, "awaiting_input")
        return dict(row.payload) if row is not None else None

    # ---- what the run wrote ---------------------------------------------------------

    async def list_events(
        self, run_id: uuid.UUID, after_id: int = 0, limit: int = 200
    ) -> list[Any]:
        """A page of a run's events, oldest first, starting after `after_id`."""
        await self.get_run(run_id)
        async with session(self.engine) as s:
            return await db.list_events(s, run_id, after_id=after_id, limit=min(max(limit, 1), 500))

    async def list_artifacts(self, run_id: uuid.UUID) -> list[Any]:
        """Every artifact the run wrote, oldest first. Content included — callers that
        only want sizes take them from the rows they get."""
        await self.get_run(run_id)
        async with session(self.engine) as s:
            return await db.list_artifacts(s, run_id)

    async def get_artifact(self, run_id: uuid.UUID, kind: str) -> Any:
        """The latest artifact of one kind, or `NotFound`.

        Two different misses, deliberately distinguished in the message: no such run, and
        a real run that never wrote this kind. A caller polling for `diff` needs to know
        which, because only one of them is worth retrying.
        """
        await self.get_run(run_id)
        async with session(self.engine) as s:
            row = await db.latest_artifact(s, run_id, kind)
        if row is None:
            raise NotFound(f"run has no {kind!r} artifact")
        return row

    # ---- the human in the loop ------------------------------------------------------

    async def answer(self, run_id: uuid.UUID, text: str, tool_call_id: str | None = None) -> None:
        """Deliver a human's answer to a run parked on an open question."""
        row = await self.get_run(run_id)
        if row.status != "awaiting_input":
            raise Conflict(f"run is {row.status}, not awaiting input")
        message: dict[str, Any] = {"type": "answer", "text": text}
        if tool_call_id:
            message["tool_call_id"] = tool_call_id
        await self.bus.push_inbox(run_id, message)
        log.info("answer_posted", run_id=str(run_id))

    async def approve(self, run_id: uuid.UUID, tool_call_id: str) -> None:
        """Let the tool call the run is parked on proceed."""
        await self._pending(run_id, tool_call_id)
        await self.bus.push_inbox(run_id, {"type": "approve", "tool_call_id": tool_call_id})
        log.info("approved", run_id=str(run_id), tool_call_id=tool_call_id)

    async def reject(self, run_id: uuid.UUID, tool_call_id: str, reason: str) -> None:
        """Refuse the call. The reason goes back to the model as the tool's result."""
        await self._pending(run_id, tool_call_id)
        await self.bus.push_inbox(
            run_id, {"type": "reject", "tool_call_id": tool_call_id, "reason": reason}
        )
        log.info("rejected", run_id=str(run_id), reason=reason[:120])

    async def cancel(self, run_id: uuid.UUID) -> None:
        """Ask a run to stop. It ends at the next node or tool call, whichever comes first."""
        await self.get_run(run_id)
        await self.bus.set_cancel(run_id)
        log.info("cancel_requested", run_id=str(run_id))

    async def _pending(self, run_id: uuid.UUID, tool_call_id: str) -> None:
        """A decision is only meaningful for the call the run is actually waiting on.

        Without this an approval could be replayed later against a different call, which
        is how a human ends up authorising something they never saw. It lives here rather
        than in a transport so that every way of approving goes through it — the reason
        this module exists at all.
        """
        row = await self.get_run(run_id)
        if row.status != "awaiting_input":
            raise Conflict(f"run is {row.status}, not awaiting a decision")
        pending = await self.bus.get_pending(run_id)
        if pending != tool_call_id:
            raise Conflict(f"the run is waiting on {pending or 'nothing'}, not {tool_call_id}")
