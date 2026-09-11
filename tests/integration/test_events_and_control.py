"""SSE replay semantics, and the answer/cancel endpoints."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from api.main import create_app
from contracts import Budget
from core.settings import load_settings
from orchestrator.events import emit, shrink
from storage import repo as db
from storage.db import session
from storage.redis import RedisBus

pytestmark = pytest.mark.integration
KEY = "test-key-123456"


class FakeArq:
    async def enqueue_job(self, name: str, run_id: str, **kw: Any) -> None:
        return None


@pytest.fixture
async def api(engine: AsyncEngine, redis_url: str) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(load_settings(env_file=None))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.engine = engine
        app.state.arq = FakeArq()
        await app.state.bus.r.flushdb()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def make_run(engine: AsyncEngine, status: str = "running") -> uuid.UUID:
    async with session(engine) as s:
        run_id = await db.create_run(
            s,
            repo_url="https://github.com/acme/demo",
            base_branch="main",
            goal="do the thing",
            budget=Budget(),
        )
        await db.set_run_phase(s, run_id, "code", status)
    return run_id


def test_shrink_caps_the_two_fields_that_can_carry_a_file() -> None:
    payload = shrink({"input": {"file_text": "x" * 5000}, "output": "y" * 5000, "name": "bash"})
    assert "+2952 chars" in payload["input"]["file_text"]
    assert len(payload["output"]) < 1100
    assert payload["name"] == "bash"  # other fields are untouched


async def test_finished_run_replays_from_the_table_and_closes(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The Redis stream is capped; the table is the source of truth for a finished run."""
    run_id = await make_run(engine, status="done")
    for phase in ("analyze", "plan", "code"):
        await emit(None, engine, run_id, "phase_changed", {"phase": phase})

    async with api.stream("GET", f"/runs/{run_id}/events", headers={"X-API-Key": KEY}) as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert body.count("event: phase_changed") == 3
    assert '"phase":"analyze"' in body.replace(" ", "")


async def test_reconnecting_with_last_event_id_replays_only_what_was_missed(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = await make_run(engine, status="done")
    for phase in ("analyze", "plan", "code"):
        await emit(None, engine, run_id, "phase_changed", {"phase": phase})
    async with session(engine) as s:
        rows = await db.list_events(s, run_id)
    first_id = rows[0].id

    async with api.stream(
        "GET",
        f"/runs/{run_id}/events",
        headers={"X-API-Key": KEY, "Last-Event-ID": str(first_id)},
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert body.count("event: phase_changed") == 2
    assert "analyze" not in body  # the one we already had is not sent again


async def test_events_require_a_key_and_a_real_run(
    api: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = await make_run(engine, status="done")
    assert (await api.get(f"/runs/{run_id}/events")).status_code == 401
    missing = await api.get(f"/runs/{uuid.uuid4()}/events", headers={"X-API-Key": KEY})
    assert missing.status_code == 404


async def test_answer_is_delivered_only_while_the_run_waits(
    api: httpx.AsyncClient, engine: AsyncEngine, redis_url: str
) -> None:
    run_id = await make_run(engine, status="running")
    busy = await api.post(
        f"/runs/{run_id}/answer", json={"text": "JWT"}, headers={"X-API-Key": KEY}
    )
    assert busy.status_code == 409 and "not awaiting input" in busy.json()["detail"]

    async with session(engine) as s:
        await db.set_run_phase(s, run_id, "awaiting_input", "awaiting_input")
    accepted = await api.post(
        f"/runs/{run_id}/answer", json={"text": "JWT HS256"}, headers={"X-API-Key": KEY}
    )
    assert accepted.status_code == 202

    bus = RedisBus(redis_url)
    try:
        assert await bus.pop_inbox(run_id, timeout_s=2) == {"type": "answer", "text": "JWT HS256"}
    finally:
        await bus.close()

    bad = await api.post(f"/runs/{run_id}/answer", json={"text": ""}, headers={"X-API-Key": KEY})
    assert bad.status_code == 422


async def test_cancel_sets_the_flag_the_runner_polls(
    api: httpx.AsyncClient, engine: AsyncEngine, redis_url: str
) -> None:
    run_id = await make_run(engine)
    assert (await api.post(f"/runs/{run_id}/cancel", headers={"X-API-Key": KEY})).status_code == 202

    bus = RedisBus(redis_url)
    try:
        assert await bus.is_cancelled(run_id)
    finally:
        await bus.close()

    missing = await api.post(f"/runs/{uuid.uuid4()}/cancel", headers={"X-API-Key": KEY})
    assert missing.status_code == 404
