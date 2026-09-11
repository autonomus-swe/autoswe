"""Answering a parked run, and cancelling a running one."""

from __future__ import annotations

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
    await request.app.state.bus.push_inbox(run_id, {"type": "answer", "text": body.text})
    log.info("answer_posted", run_id=str(run_id))
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
