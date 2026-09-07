"""Async engine and session helpers. One lazy engine per process; tests build their own."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.settings import get_settings

_engine: AsyncEngine | None = None


def make_engine(url: str, **kwargs: Any) -> AsyncEngine:
    kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(url, **kwargs)


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().database_url, pool_size=10, max_overflow=5)
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def session_factory(eng: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(eng or engine(), expire_on_commit=False)


@asynccontextmanager
async def session(eng: AsyncEngine | None = None) -> AsyncIterator[AsyncSession]:
    """Commit on success, roll back on any exception."""
    async with session_factory(eng)() as s:
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise
