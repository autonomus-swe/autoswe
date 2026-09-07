"""Create and read runs."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.auth import rate_limit
from api.schemas import RunAccepted, RunCreate, RunSummary
from contracts import Budget
from observability.logging import get_logger
from storage import repo as db
from storage.db import session

log = get_logger(__name__)
router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=RunAccepted)
async def create_run(
    body: RunCreate, request: Request, _key: str = Depends(rate_limit)
) -> RunAccepted:
    async with session(request.app.state.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=body.repo_url,
            base_branch=body.base_branch,
            goal=body.goal,
            budget=body.budget or Budget(),
            provider=request.app.state.settings.llm_provider,
        )
    pool = request.app.state.arq
    if pool is not None:
        await pool.enqueue_job("run_job", str(run_id), _job_id=str(run_id))
    else:  # the queue is unavailable: the run stays queued rather than silently vanishing
        log.warning("arq_unavailable", run_id=str(run_id))
    log.info("run_created", run_id=str(run_id), repo_url=body.repo_url)
    return RunAccepted(run_id=run_id)


@router.get("/{run_id}", response_model=RunSummary)
async def get_run(run_id: UUID, request: Request, _key: str = Depends(rate_limit)) -> RunSummary:
    async with session(request.app.state.engine) as s:
        row = await db.get_run(s, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
    return RunSummary.from_row(row)
