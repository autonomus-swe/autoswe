"""Control plane against a real Postgres and Redis; the job queue is stubbed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from api.main import create_app
from core.settings import load_settings

pytestmark = pytest.mark.integration
KEY = "test-key-123456"


class FakeArq:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str]] = []

    async def enqueue_job(self, name: str, run_id: str, **kw: Any) -> None:
        self.jobs.append((name, run_id))


@pytest.fixture
async def api(
    engine: AsyncEngine, redis_url: str
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeArq]]:
    app = create_app(load_settings(env_file=None))
    arq = FakeArq()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.engine = engine  # reuse the migrated test database
        app.state.arq = arq
        await app.state.bus.r.flushdb()  # every test starts with a full rate-limit bucket
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, arq


async def test_healthz_reports_dependencies(api: tuple[httpx.AsyncClient, FakeArq]) -> None:
    client, _ = api
    res = await client.get("/healthz")
    assert res.status_code == 200 and res.json()["checks"] == {"database": "ok", "redis": "ok"}


async def test_create_run_requires_a_valid_key(api: tuple[httpx.AsyncClient, FakeArq]) -> None:
    client, arq = api
    body = {"repo_url": "https://github.com/acme/demo", "goal": "Implement subtract(a, b)"}
    for headers in ({}, {"X-API-Key": "wrong-key-000000"}):
        res = await client.post("/runs", json=body, headers=headers)
        assert res.status_code == 401 and res.json() == {"detail": "unauthorized"}
    assert arq.jobs == []


async def test_create_run_enqueues_and_is_readable(api: tuple[httpx.AsyncClient, FakeArq]) -> None:
    client, arq = api
    body = {"repo_url": "https://github.com/acme/demo", "goal": "Implement subtract(a, b)"}
    res = await client.post("/runs", json=body, headers={"X-API-Key": KEY})
    assert res.status_code == 202
    run_id = res.json()["run_id"]
    assert arq.jobs == [("run_job", run_id)]

    got = await client.get(f"/runs/{run_id}", headers={"X-API-Key": KEY})
    assert got.status_code == 200
    summary = got.json()
    assert summary["status"] == "queued" and summary["phase"] == "setup"
    assert summary["work_branch"] == f"agent/{run_id}" and summary["cost_usd"] == 0.0
    assert summary["pr_url"] is None and summary["goal"] == body["goal"]

    missing = await client.get(
        f"/runs/{'0' * 8}-0000-0000-0000-{'0' * 12}", headers={"X-API-Key": KEY}
    )
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"repo_url": "https://gitlab.com/acme/demo", "goal": "Implement subtract(a, b)"},
        {"repo_url": "https://github.com/acme", "goal": "Implement subtract(a, b)"},
        {"repo_url": "https://github.com/acme/demo", "goal": "short"},
        {
            "repo_url": "https://github.com/acme/demo",
            "goal": "Implement subtract(a, b)",
            "extra": 1,
        },
        {
            "repo_url": "https://github.com/acme/demo",
            "goal": "Implement subtract",
            "base_branch": "a b",
        },
    ],
)
async def test_invalid_bodies_are_rejected(
    api: tuple[httpx.AsyncClient, FakeArq], body: dict[str, Any]
) -> None:
    client, _ = api
    res = await client.post("/runs", json=body, headers={"X-API-Key": KEY})
    assert res.status_code == 422


async def test_rate_limit_after_the_burst(api: tuple[httpx.AsyncClient, FakeArq]) -> None:
    client, _ = api
    codes = [
        (
            await client.get(
                f"/runs/{'0' * 8}-0000-0000-0000-{'0' * 12}", headers={"X-API-Key": KEY}
            )
        ).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [404] * 5 and codes[5] == 429
