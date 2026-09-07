"""Real Postgres (pgvector) and Redis via testcontainers. Session-scoped containers,
per-test engine with NullPool so connections never cross event loops."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from core.settings import get_settings
from storage.db import make_engine, session
from storage.migrate import upgrade
from storage.models import CORE_TABLES
from storage.redis import RedisBus


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch, pg_url: str, redis_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("API_KEYS", "test-key-123456")
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def migrated_pg_url(pg_url: str) -> str:
    upgrade(pg_url)
    return pg_url


@pytest.fixture
async def engine(migrated_pg_url: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(migrated_pg_url, poolclass=NullPool)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.execute(text("TRUNCATE " + ", ".join(CORE_TABLES) + " CASCADE"))
        await eng.dispose()


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with session(engine) as s:
        yield s


@pytest.fixture
async def bus(redis_url: str) -> AsyncIterator[RedisBus]:
    b = RedisBus(redis_url)
    await b.r.flushdb()
    try:
        yield b
    finally:
        await b.close()
