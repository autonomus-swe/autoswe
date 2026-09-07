from __future__ import annotations

import asyncio
import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from contracts import Budget, TaskGraph, TaskGraphSpec, TaskSpec, Usage
from storage import repo
from storage.db import make_engine
from storage.migrate import downgrade, upgrade
from storage.models import CORE_TABLES, Base

pytestmark = pytest.mark.integration


async def test_migration_creates_core_tables_and_vector_extension(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        ext = (
            await conn.execute(text("select extname from pg_extension where extname = 'vector'"))
        ).scalar()
    assert set(CORE_TABLES) <= set(names)
    assert ext == "vector"


async def test_handwritten_migration_matches_models(engine: AsyncEngine) -> None:
    def diff(conn: Connection) -> list[object]:
        ctx = MigrationContext.configure(conn)
        return list(compare_metadata(ctx, Base.metadata))

    async with engine.connect() as conn:
        diffs = await conn.run_sync(diff)
    assert diffs == [], f"schema drift between migration and models: {diffs}"


def _graph(*ids: str) -> TaskGraph:
    specs = [
        TaskSpec(
            id=i,
            title=i,
            description="d",
            depends_on=[],
            files=["a.py"],
            acceptance_criteria=["ok"],
            test_selector="tests/",
        )
        for i in ids
    ]
    return TaskGraph.from_spec(TaskGraphSpec(tasks=specs))


async def test_run_lifecycle_and_cost_from_llm_calls(db: AsyncSession) -> None:
    run_id = await repo.create_run(
        db,
        repo_url="https://github.com/x/y",
        base_branch="main",
        goal="do the thing",
        budget=Budget(max_usd=5),
    )
    row = await repo.get_run(db, run_id)
    assert row is not None
    assert (
        row.work_branch == f"agent/{run_id}"
        and row.status == "queued"
        and row.budget["max_usd"] == 5.0
    )

    await repo.mark_run_started(db, run_id)
    await repo.set_run_phase(db, run_id, "code", "running")

    step = await repo.start_step(
        db, run_id=run_id, task_id="t1", agent="coder", phase="code", input={"task": "t1"}
    )
    await repo.insert_llm_call(
        db,
        step_id=step,
        provider="anthropic",
        model="claude-opus-5",
        effort="xhigh",
        usage=Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=800, cost_usd=0.0123),
        latency_ms=900,
        stop_reason="tool_use",
    )
    await repo.insert_llm_call(
        db,
        step_id=step,
        provider="anthropic",
        model="claude-opus-5",
        effort="xhigh",
        usage=Usage(input_tokens=500, output_tokens=100, cache_write_tokens=50, cost_usd=0.0077),
        latency_ms=700,
        stop_reason="end_turn",
    )
    await repo.insert_tool_call(
        db,
        step_id=step,
        name="bash",
        input={"command": "ls"},
        output_preview="a.py",
        exit_code=0,
        duration_ms=12,
    )

    cost = await repo.run_cost(db, run_id)
    assert (
        cost.input_tokens,
        cost.output_tokens,
        cost.cache_read_tokens,
        cost.cache_write_tokens,
    ) == (1500, 300, 800, 50)
    assert cost.cost_usd == pytest.approx(0.02)

    await repo.finish_step(db, step, output={"summary": "ok"}, error=None, usage=cost)
    await repo.set_run_cost(db, run_id, cost.cost_usd)
    await repo.finish_run(db, run_id, status="done", pr_url="https://github.com/x/y/pull/1")
    row = await repo.get_run(db, run_id)
    assert row is not None and row.status == "done" and float(row.cost_usd) == pytest.approx(0.02)
    assert row.finished_at is not None and row.started_at is not None


async def test_checkpoints_events_artifacts(db: AsyncSession) -> None:
    run_id = await repo.create_run(db, repo_url="u", base_branch="main", goal="g", budget=Budget())
    assert await repo.latest_checkpoint(db, run_id) is None
    await repo.save_checkpoint(db, run_id, 1, "setup", {"phase": "setup"})
    await repo.save_checkpoint(db, run_id, 2, "analyze", {"phase": "analyze", "n": 2})
    assert await repo.latest_checkpoint(db, run_id) == (2, {"phase": "analyze", "n": 2})

    first = await repo.insert_event(db, run_id, "phase_changed", {"phase": "setup"})
    await repo.insert_event(db, run_id, "phase_changed", {"phase": "analyze"})
    events = await repo.list_events(db, run_id, after_id=first)
    assert [e.payload["phase"] for e in events] == ["analyze"]

    await repo.save_artifact(db, run_id, "diff", None, {"text": "old"})
    await repo.save_artifact(db, run_id, "diff", None, {"text": "new"})
    latest = await repo.latest_artifact(db, run_id, "diff")
    assert latest is not None and latest.content["text"] == "new"
    assert await repo.latest_artifact(db, run_id, "missing") is None


async def test_upsert_tasks_is_idempotent_and_updates(db: AsyncSession) -> None:
    run_id = await repo.create_run(db, repo_url="u", base_branch="main", goal="g", budget=Budget())
    graph = _graph("t1", "t2")
    await repo.upsert_tasks(db, run_id, graph)
    graph.by_id("t1").status = "done"
    graph.by_id("t1").attempts = 2
    await repo.upsert_tasks(db, run_id, graph)
    rows = await repo.list_tasks(db, run_id)
    assert [(r.id, r.status, r.attempts) for r in rows] == [("t1", "done", 2), ("t2", "pending", 0)]


async def test_cascade_delete_run_removes_children(db: AsyncSession) -> None:
    run_id = await repo.create_run(db, repo_url="u", base_branch="main", goal="g", budget=Budget())
    step = await repo.start_step(db, run_id=run_id, task_id=None, agent="planner", phase="plan")
    await repo.insert_llm_call(
        db,
        step_id=step,
        provider="anthropic",
        model="m",
        effort=None,
        usage=Usage(),
        latency_ms=1,
        stop_reason=None,
    )
    await db.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run_id})
    remaining = (await db.execute(text("SELECT count(*) FROM llm_calls"))).scalar()
    assert remaining == 0


async def test_unknown_run_is_none(db: AsyncSession) -> None:
    assert await repo.get_run(db, uuid.uuid4()) is None


async def test_downgrade_removes_tables_then_upgrade_restores(migrated_pg_url: str) -> None:
    await asyncio.to_thread(downgrade, migrated_pg_url, "base")
    eng = make_engine(migrated_pg_url)
    try:
        async with eng.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert not (set(CORE_TABLES) & set(names))
    finally:
        await eng.dispose()
    await asyncio.to_thread(upgrade, migrated_pg_url, "head")
