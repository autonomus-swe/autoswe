"""Answering a parked run, and cancelling a running one.

Every route here is a shape conversion: parse the body, call `api.service`, turn a
`ControlError` into a status code. The preconditions — is the run real, is it awaiting
input, is this the call it is actually waiting on — live in the service so that the MCP
server enforces the same ones without a second copy to keep in step.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from api.auth import rate_limit
from api.errors import http_error
from api.service import ControlError, plane_from
from observability.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/runs", tags=["control"])

ACCEPTED = {"status": "accepted"}


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    # Set when answering an `ask_user` call rather than the Planner's open questions. The
    # gate matches on it, so an answer without one is for the phase, not for a tool.
    tool_call_id: str | None = Field(default=None, max_length=200)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=200)


class Rejection(Approval):
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/{run_id}/answer", status_code=status.HTTP_202_ACCEPTED)
async def answer(
    run_id: UUID, body: Answer, request: Request, _key: str = Depends(rate_limit)
) -> dict[str, str]:
    """Deliver a human's answer to a run parked on an open question."""
    try:
        await plane_from(request.app.state).answer(run_id, body.text, body.tool_call_id)
    except ControlError as e:
        raise http_error(e) from e
    return ACCEPTED


@router.post("/{run_id}/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve(
    run_id: UUID, body: Approval, request: Request, _key: str = Depends(rate_limit)
) -> dict[str, str]:
    """Let the tool call the run is parked on proceed."""
    try:
        await plane_from(request.app.state).approve(run_id, body.tool_call_id)
    except ControlError as e:
        raise http_error(e) from e
    return ACCEPTED


@router.post("/{run_id}/reject", status_code=status.HTTP_202_ACCEPTED)
async def reject(
    run_id: UUID, body: Rejection, request: Request, _key: str = Depends(rate_limit)
) -> dict[str, str]:
    """Refuse the call. The reason goes back to the model as the tool's result."""
    try:
        await plane_from(request.app.state).reject(run_id, body.tool_call_id, body.reason)
    except ControlError as e:
        raise http_error(e) from e
    return ACCEPTED


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel(run_id: UUID, request: Request, _key: str = Depends(rate_limit)) -> dict[str, str]:
    """Ask a run to stop. It ends at the next node or tool call, whichever comes first."""
    try:
        await plane_from(request.app.state).cancel(run_id)
    except ControlError as e:
        raise http_error(e) from e
    return ACCEPTED
