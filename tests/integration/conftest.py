"""Per-test database engine and Redis bus on the shared containers from tests/conftest.py."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import NullPool

from core.settings import get_settings
from repo.gitcmd import git
from storage.db import make_engine, session
from storage.models import CORE_TABLES
from storage.redis import RedisBus


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch, pg_url: str, redis_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("API_KEYS", "test-key-123456")
    get_settings.cache_clear()


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


FIXTURE_SRC = Path(__file__).resolve().parents[1] / "fixtures" / "fixture_repo"


@pytest.fixture
def origin_repo(host_tmp: Path) -> Iterator[Path]:
    import asyncio

    src = host_tmp / "origin"
    src.mkdir()
    shutil.copytree(
        FIXTURE_SRC,
        src,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", ".pytest_cache"),
    )

    async def init() -> None:
        await git("init", "-q", "-b", "main", cwd=src)
        await git("add", "-A", cwd=src)
        await git("commit", "-q", "-m", "chore: fixture project", cwd=src)

    asyncio.run(init())
    yield src
