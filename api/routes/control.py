"""Answering a parked run, and cancelling a running one."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from api.auth import rate_limit
from observability.logging import get_logger
from storage import repo as db
from storage.db import session

log = get_logger(__name__)
router = APIRouter(prefix="/runs", tags=["control"])


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    # Set when answering an `ask_user` call rather than the Planner's open questions. The
    # gate matches on it, so an answer without one is for the phase, not for a tool.
    tool_call_id: str | None = Field(default=None, max_length=200)


@router.post("/{run_id}/answer", status_code=status.HTTP_202_ACCEPTED)
async def answer(
    run_id: UUID, body: Answer, request: Request, _key: str = Depends(rate_limit)
) -> dict[str, str]:
    """Deliver a human's answer to a run parked on an open question."""
    async with session(request.app.state.engine) as s:
        row = await db.get_run(s, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
    if row.status != "awaiting_input":
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"run is {row.status}, not awaiting input"
        )
    message: dict[str, Any] = {"type": "answer", "text": body.text}
    if body.tool_call_id:
        message["tool_call_id"] = body.tool_call_id
    await request.app.state.bus.push_inbox(run_id, message)
    log.info("answer_posted", run_id=str(run_id))
    return {"status": "accepted"}


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=200)


class Rejection(Approval):
    reason: str = Field(min_length=1, max_length=1000)


async def _pending_or_conflict(request: Request, run_id: UUID, tool_call_id: str) -> None:
    """A decision is only meaningful for the call the run is actually waiting on.

    Without this an approval could be replayed later against a different call, which is
    how a human ends up authorising something they never saw.
    """
    async with session(request.app.state.engine) as s:
        row = await db.get_run(s, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
    if row.status != "awaiting_input":
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"run is {row.status}, not awaiting a decision"
        )
    pending = await request.app.state.bus.get_pending(run_id)
    if pending != tool_call_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"the run is waiting on {pending or 'nothing'}, not {tool_call_id}",
        )


@router.post("/{run_id}/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve(
    run_id: UUID, body: Approval, request: Request, _key: str = Depends(rate_limit)
) -> dict[str, str]:
    """Let the tool call the run is parked on proceed."""
    await _pending_or_conflict(request, run_id, body.tool_call_id)
    await request.app.state.bus.push_inbox(
        run_id, {"type": "approve", "tool_call_id": body.tool_call_id}
    )
    log.info("approved", run_id=str(run_id), tool_call_id=body.tool_call_id)
    return {"status": "accepted"}


@router.post("/{run_id}/reject", status_code=status.HTTP_202_ACCEPTED)
async def reject(
    run_id: UUID, body: Rejection, request: Request, _key: str = Depends(rate_limit)
) -> dict[str, str]:
    """Refuse the call. The reason goes back to the model as the tool's result."""
    await _pending_or_conflict(request, run_id, body.tool_call_id)
    await request.app.state.bus.push_inbox(
        run_id,
        {"type": "reject", "tool_call_id": body.tool_call_id, "reason": body.reason},
    )
    log.info("rejected", run_id=str(run_id), reason=body.reason[:120])
    return {"status": "accepted"}


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel(run_id: UUID, request: Request, _key: str = Depends(rate_limit)) -> dict[str, str]:
    """Ask a run to stop. It ends at the next node or tool call, whichever comes first."""
    async with session(request.app.state.engine) as s:
        row = await db.get_run(s, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
    await request.app.state.bus.set_cancel(run_id)
    log.info("cancel_requested", run_id=str(run_id))
    return {"status": "accepted"}
