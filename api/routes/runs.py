"""Create and read runs."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.auth import rate_limit, read_rate_limit
from api.schemas import (
    EventView,
    LLMCallView,
    RunAccepted,
    RunCreate,
    RunDetail,
    RunSummary,
    StepView,
    TaskView,
    ToolCallView,
)
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


@router.get("", response_model=list[RunSummary])
async def list_runs(
    request: Request, limit: int = 50, _key: str = Depends(read_rate_limit)
) -> list[RunSummary]:
    """Most recent runs first — what the dashboard opens on."""
    async with session(request.app.state.engine) as s:
        rows = await db.list_runs(s, limit=min(max(limit, 1), 200))
    return [RunSummary.from_row(r) for r in rows]


@router.get("/{run_id}/detail", response_model=RunDetail)
async def run_detail(
    run_id: UUID, request: Request, _key: str = Depends(read_rate_limit)
) -> RunDetail:
    """One run in full: tasks, steps, every tool call and model turn, and the totals."""
    async with session(request.app.state.engine) as s:
        row = await db.get_run(s, run_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
        tasks = await db.list_tasks(s, run_id)
        steps = await db.list_steps(s, run_id)
        events = await db.list_events(s, run_id)
        tool_calls = await db.list_tool_calls(s, run_id)
        llm_calls = await db.list_llm_calls(s, run_id)
        usage = await db.run_cost(s, run_id)
    return RunDetail(
        run=RunSummary.from_row(row),
        tasks=[TaskView.from_row(t) for t in tasks],
        steps=[
            StepView(
                id=st.id,
                agent=st.agent,
                phase=st.phase,
                task_id=st.task_id,
                attempt=st.attempt,
                error=st.error,
                started_at=st.started_at,
                finished_at=st.finished_at,
            )
            for st in steps
        ],
        events=[EventView(id=e.id, type=e.type, payload=e.payload, ts=e.ts) for e in events],
        tool_calls=[
            ToolCallView(
                seq=tc.seq,
                name=tc.name,
                exit_code=tc.exit_code,
                duration_ms=tc.duration_ms,
                input=tc.input,
                output_preview=tc.output_preview,
            )
            for tc in tool_calls
        ],
        llm_calls=[
            LLMCallView(
                seq=lc.seq,
                model=lc.model,
                effort=lc.effort,
                input_tokens=lc.input_tokens,
                output_tokens=lc.output_tokens,
                cost_usd=float(lc.cost_usd),
                latency_ms=lc.latency_ms,
                stop_reason=lc.stop_reason,
            )
            for lc in llm_calls
        ],
        totals={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "tool_calls": len(tool_calls),
            "llm_calls": len(llm_calls),
        },
    )


@router.get("/{run_id}", response_model=RunSummary)
async def get_run(
    run_id: UUID, request: Request, _key: str = Depends(read_rate_limit)
) -> RunSummary:
    async with session(request.app.state.engine) as s:
        row = await db.get_run(s, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
    return RunSummary.from_row(row)
