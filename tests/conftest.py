"""Root conftest. The container fixtures live here so integration and end-to-end tests
can share them; they start lazily, so `make test` (unit only) never launches Docker."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def migrated_pg_url(pg_url: str) -> str:
    from storage.migrate import upgrade

    upgrade(pg_url)
    return pg_url


@pytest.fixture
def host_tmp() -> Iterator[Path]:
    """Scratch dir under $HOME (or AUTOSWE_TEST_TMP): snap-packaged Docker cannot bind /tmp."""
    base = Path(os.environ.get("AUTOSWE_TEST_TMP", Path.home() / ".autoswe" / "tmp"))
    base.mkdir(parents=True, exist_ok=True)
    d = base / uuid.uuid4().hex
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Undo any ``configure_logging`` a test performed.

    structlog binds its logger to the stream live at configure time. Under pytest that is
    the current test's captured stdout, which is closed at teardown, so the next test to
    log would raise ``I/O operation on closed file``.
    """
    yield
    structlog.reset_defaults()
