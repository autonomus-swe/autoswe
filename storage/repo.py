"""Thin, explicit query functions. No ORM relationships leak upward; callers pass a session."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.budget import Budget, Usage
from contracts.plan import TaskGraph
from storage.models import (
    ArtifactRow,
    CheckpointRow,
    EventRow,
    LLMCallRow,
    RepoSymbolRow,
    RunRow,
    StepRow,
    TaskRow,
    ToolCallRow,
)

# ---- runs -------------------------------------------------------------------


async def create_run(
    s: AsyncSession,
    *,
    repo_url: str,
    base_branch: str,
    goal: str,
    budget: Budget,
    provider: str = "anthropic",
    unattended: bool = False,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    s.add(
        RunRow(
            id=run_id,
            repo_url=repo_url,
            base_branch=base_branch,
            work_branch=f"agent/{run_id}",
            goal=goal,
            phase="setup",
            status="queued",
            budget=budget.model_dump(mode="json"),
            provider=provider,
            unattended=unattended,
        )
    )
    await s.flush()
    return run_id


async def list_runs(s: AsyncSession, limit: int = 50) -> list[RunRow]:
    """Most recent runs first."""
    res = await s.execute(select(RunRow).order_by(RunRow.created_at.desc()).limit(limit))
    return list(res.scalars())


async def list_steps(s: AsyncSession, run_id: uuid.UUID) -> list[StepRow]:
    """Every agent invocation of a run, in order."""
    res = await s.execute(
        select(StepRow).where(StepRow.run_id == run_id).order_by(StepRow.started_at)
    )
    return list(res.scalars())


async def get_run(s: AsyncSession, run_id: uuid.UUID) -> RunRow | None:
    return await s.get(RunRow, run_id)


async def set_run_phase(s: AsyncSession, run_id: uuid.UUID, phase: str, status: str) -> None:
    await s.execute(update(RunRow).where(RunRow.id == run_id).values(phase=phase, status=status))


async def set_run_status(s: AsyncSession, run_id: uuid.UUID, status: str) -> None:
    """Status only. An approval pauses a run without moving it to another phase."""
    await s.execute(update(RunRow).where(RunRow.id == run_id).values(status=status))


async def mark_run_started(s: AsyncSession, run_id: uuid.UUID) -> None:
    await s.execute(
        update(RunRow)
        .where(RunRow.id == run_id, RunRow.started_at.is_(None))
        .values(started_at=datetime.now(UTC), status="running")
    )


async def finish_run(
    s: AsyncSession,
    run_id: uuid.UUID,
    *,
    status: str,
    pr_url: str | None = None,
    error: str | None = None,
) -> None:
    await s.execute(
        update(RunRow)
        .where(RunRow.id == run_id)
        .values(status=status, pr_url=pr_url, error=error, finished_at=datetime.now(UTC))
    )


async def set_run_cost(s: AsyncSession, run_id: uuid.UUID, cost_usd: float) -> None:
    await s.execute(
        update(RunRow).where(RunRow.id == run_id).values(cost_usd=Decimal(str(round(cost_usd, 4))))
    )


# ---- tasks ------------------------------------------------------------------


async def upsert_tasks(s: AsyncSession, run_id: uuid.UUID, graph: TaskGraph) -> None:
    rows = [
        {
            "run_id": run_id,
            "id": t.id,
            "title": t.spec.title,
            "description": t.spec.description,
            "depends_on": t.spec.depends_on,
            "files": t.spec.files,
            "acceptance_criteria": t.spec.acceptance_criteria,
            "test_selector": t.spec.test_selector,
            "status": t.status,
            "attempts": t.attempts,
            "replanned": t.replanned,
        }
        for t in graph.tasks
    ]
    if not rows:
        return
    stmt = pg_insert(TaskRow).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="pk_tasks",
        set_={
            c: getattr(stmt.excluded, c)
            for c in (
                "title",
                "description",
                "depends_on",
                "files",
                "acceptance_criteria",
                "test_selector",
                "status",
                "attempts",
                "replanned",
            )
        },
    )
    await s.execute(stmt)


async def list_tasks(s: AsyncSession, run_id: uuid.UUID) -> list[TaskRow]:
    res = await s.execute(select(TaskRow).where(TaskRow.run_id == run_id).order_by(TaskRow.id))
    return list(res.scalars())


# ---- steps / tool calls / llm calls -----------------------------------------


async def start_step(
    s: AsyncSession,
    *,
    run_id: uuid.UUID,
    task_id: str | None,
    agent: str,
    phase: str,
    input: dict[str, Any] | None = None,
    attempt: int = 0,
) -> uuid.UUID:
    step_id = uuid.uuid4()
    s.add(
        StepRow(
            id=step_id,
            run_id=run_id,
            task_id=task_id,
            agent=agent,
            phase=phase,
            input=input,
            attempt=attempt,
        )
    )
    await s.flush()
    return step_id


async def finish_step(
    s: AsyncSession,
    step_id: uuid.UUID,
    *,
    output: dict[str, Any] | None,
    error: str | None,
    usage: Usage,
) -> None:
    await s.execute(
        update(StepRow)
        .where(StepRow.id == step_id)
        .values(
            output=output,
            error=error,
            usage=usage.model_dump(mode="json"),
            finished_at=datetime.now(UTC),
        )
    )


async def insert_tool_call(
    s: AsyncSession,
    *,
    step_id: uuid.UUID,
    name: str,
    input: dict[str, Any],
    output_preview: str | None,
    exit_code: int | None,
    duration_ms: int,
    approved_by: str | None = None,
) -> uuid.UUID:
    row = ToolCallRow(
        step_id=step_id,
        name=name,
        input=input,
        output_preview=output_preview,
        exit_code=exit_code,
        duration_ms=duration_ms,
        approved_by=approved_by,
    )
    s.add(row)
    await s.flush()
    return row.id


async def insert_llm_call(
    s: AsyncSession,
    *,
    step_id: uuid.UUID,
    provider: str,
    model: str,
    effort: str | None,
    usage: Usage,
    latency_ms: int,
    stop_reason: str | None,
) -> uuid.UUID:
    row = LLMCallRow(
        step_id=step_id,
        provider=provider,
        model=model,
        effort=effort,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=Decimal(str(round(usage.cost_usd, 6))),
        latency_ms=latency_ms,
        stop_reason=stop_reason,
    )
    s.add(row)
    await s.flush()
    return row.id


async def list_tool_calls(s: AsyncSession, run_id: uuid.UUID) -> list[ToolCallRow]:
    """Every tool call of a run, in the order it happened."""
    res = await s.execute(
        select(ToolCallRow)
        .join(StepRow, StepRow.id == ToolCallRow.step_id)
        .where(StepRow.run_id == run_id)
        .order_by(ToolCallRow.seq)
    )
    return list(res.scalars())


async def list_llm_calls(s: AsyncSession, run_id: uuid.UUID) -> list[LLMCallRow]:
    """Every model turn of a run, in the order it happened."""
    res = await s.execute(
        select(LLMCallRow)
        .join(StepRow, StepRow.id == LLMCallRow.step_id)
        .where(StepRow.run_id == run_id)
        .order_by(LLMCallRow.seq)
    )
    return list(res.scalars())


async def run_cost(s: AsyncSession, run_id: uuid.UUID) -> Usage:
    """Budget source of truth: SUM over llm_calls of this run's steps."""
    stmt = (
        select(
            func.coalesce(func.sum(LLMCallRow.input_tokens), 0),
            func.coalesce(func.sum(LLMCallRow.output_tokens), 0),
            func.coalesce(func.sum(LLMCallRow.cache_read_tokens), 0),
            func.coalesce(func.sum(LLMCallRow.cache_write_tokens), 0),
            func.coalesce(func.sum(LLMCallRow.cost_usd), 0),
        )
        .select_from(LLMCallRow)
        .join(StepRow, StepRow.id == LLMCallRow.step_id)
        .where(StepRow.run_id == run_id)
    )
    inp, out, cr, cw, cost = (await s.execute(stmt)).one()
    return Usage(
        input_tokens=int(inp),
        output_tokens=int(out),
        cache_read_tokens=int(cr),
        cache_write_tokens=int(cw),
        cost_usd=float(cost),
    )


# ---- checkpoints / events / artifacts ---------------------------------------


async def save_checkpoint(
    s: AsyncSession, run_id: uuid.UUID, seq: int, phase: str, state: dict[str, Any]
) -> None:
    s.add(CheckpointRow(run_id=run_id, seq=seq, phase=phase, state=state))
    await s.flush()


async def latest_checkpoint(
    s: AsyncSession, run_id: uuid.UUID
) -> tuple[int, dict[str, Any]] | None:
    res = await s.execute(
        select(CheckpointRow.seq, CheckpointRow.state)
        .where(CheckpointRow.run_id == run_id)
        .order_by(CheckpointRow.seq.desc())
        .limit(1)
    )
    row = res.first()
    return (int(row[0]), dict(row[1])) if row else None


async def insert_event(
    s: AsyncSession, run_id: uuid.UUID, type: str, payload: dict[str, Any]
) -> int:
    row = EventRow(run_id=run_id, type=type, payload=payload)
    s.add(row)
    await s.flush()
    return row.id


async def list_events(
    s: AsyncSession, run_id: uuid.UUID, after_id: int = 0, limit: int = 500
) -> list[EventRow]:
    res = await s.execute(
        select(EventRow)
        .where(EventRow.run_id == run_id, EventRow.id > after_id)
        .order_by(EventRow.id)
        .limit(limit)
    )
    return list(res.scalars())


async def save_artifact(
    s: AsyncSession, run_id: uuid.UUID, kind: str, path: str | None, content: dict[str, Any]
) -> uuid.UUID:
    row = ArtifactRow(run_id=run_id, kind=kind, path=path, content=content)
    s.add(row)
    await s.flush()
    return row.id


async def latest_artifact(s: AsyncSession, run_id: uuid.UUID, kind: str) -> ArtifactRow | None:
    res = await s.execute(
        select(ArtifactRow)
        .where(ArtifactRow.run_id == run_id, ArtifactRow.kind == kind)
        .order_by(ArtifactRow.seq.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def list_artifacts(s: AsyncSession, run_id: uuid.UUID) -> list[ArtifactRow]:
    """Every artifact a run wrote, oldest first — one row per write, not per kind.

    Deliberately not deduplicated to the latest of each kind. A run writes `test_report`
    once per TEST phase and `security` once per scan, and "how many times did this run
    have to go round" is exactly what the sequence answers.
    """
    res = await s.execute(
        select(ArtifactRow).where(ArtifactRow.run_id == run_id).order_by(ArtifactRow.seq)
    )
    return list(res.scalars())


# ---- the symbol index -----------------------------------------------------------------


async def symbols_indexed(s: AsyncSession, repo_sha: str) -> bool:
    """Whether this SHA has already been indexed.

    The whole reason indexing a large repository is affordable: the index describes a
    commit, so two runs against the same base share it and a re-run is a no-op. `limit 1`
    rather than a count, because the question is existence.
    """
    res = await s.execute(
        select(RepoSymbolRow.id).where(RepoSymbolRow.repo_sha == repo_sha).limit(1)
    )
    return res.scalar_one_or_none() is not None


async def insert_symbols(s: AsyncSession, repo_sha: str, rows: list[dict[str, Any]]) -> int:
    """Bulk-insert one SHA's symbols. Returns how many landed.

    Chunked because a three-thousand-file repository is tens of thousands of rows, and a
    single statement that large is one the driver has to buffer whole.
    """
    if not rows:
        return 0
    inserted = 0
    chunk = 1000
    for start in range(0, len(rows), chunk):
        batch = [
            {**r, "id": uuid.uuid4(), "repo_sha": repo_sha} for r in rows[start : start + chunk]
        ]
        await s.execute(insert(RepoSymbolRow), batch)
        inserted += len(batch)
    return inserted


async def symbols_for_path(s: AsyncSession, repo_sha: str, path: str) -> list[RepoSymbolRow]:
    """Every definition in one file, in source order."""
    res = await s.execute(
        select(RepoSymbolRow)
        .where(RepoSymbolRow.repo_sha == repo_sha, RepoSymbolRow.path == path)
        .order_by(RepoSymbolRow.start_line)
    )
    return list(res.scalars())


async def symbols_named(
    s: AsyncSession, repo_sha: str, name: str, limit: int = 50
) -> list[RepoSymbolRow]:
    """Every definition with this name, anywhere in the tree.

    The query Step 5.2's ranking is for: a goal that names `paginate` needs to know which
    file defines it before it can put that file first.
    """
    res = await s.execute(
        select(RepoSymbolRow)
        .where(RepoSymbolRow.repo_sha == repo_sha, RepoSymbolRow.name == name)
        .order_by(RepoSymbolRow.path)
        .limit(limit)
    )
    return list(res.scalars())


async def symbols_for_sha(
    s: AsyncSession, repo_sha: str, limit: int = 100_000
) -> list[RepoSymbolRow]:
    """Every symbol at one commit, for the repo map's ranking pass.

    Bounded because a very large repository could otherwise pull hundreds of thousands of
    rows into memory to rank a map that shows a few dozen files.
    """
    res = await s.execute(
        select(RepoSymbolRow)
        .where(RepoSymbolRow.repo_sha == repo_sha)
        .order_by(RepoSymbolRow.path, RepoSymbolRow.start_line)
        .limit(limit)
    )
    return list(res.scalars())
