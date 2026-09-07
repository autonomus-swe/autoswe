"""End-to-end fixtures. These call a real model and cost money, so they are opt-in:
they run only when LLM_API_KEY is set and are selected with `-m e2e`."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from core.settings import Settings, load_settings
from orchestrator.deps import Deps
from repo.gitcmd import git

FIXTURE_SRC = Path(__file__).resolve().parents[1] / "fixtures" / "fixture_repo"
GOAL = (
    "Implement subtract(a, b) and slugify(text) in fixture/ops.py so that "
    "tests/test_ops.py passes. Do not change the tests."
)


def _docker_ready(image: str) -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(image)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def e2e_settings() -> Settings:
    settings = load_settings()
    if settings.llm_api_key is None:
        pytest.skip("LLM_API_KEY is not set; end-to-end tests are opt-in")
    if not _docker_ready(settings.sandbox_image):
        pytest.skip(f"docker or image {settings.sandbox_image} unavailable")
    return settings


@pytest.fixture
def origin_repo(e2e_settings: Settings) -> Iterator[Path]:
    """A throwaway git repository on disk holding the fixture project."""
    base = Path(os.environ.get("AUTOSWE_TEST_TMP", Path.home() / ".autoswe" / "tmp"))
    root = base / f"e2e-{uuid.uuid4().hex[:8]}"
    (root / "origin").mkdir(parents=True)
    shutil.copytree(
        FIXTURE_SRC,
        root / "origin",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", ".pytest_cache"),
    )
    try:
        import asyncio

        async def init() -> None:
            src = root / "origin"
            await git("init", "-q", "-b", "main", cwd=src)
            await git("add", "-A", cwd=src)
            await git("commit", "-q", "-m", "chore: fixture project", cwd=src)

        asyncio.run(init())
        yield root / "origin"
    finally:
        shutil.rmtree(root, ignore_errors=True)


class RecordingGitHub:
    """Stands in for PyGithub so the local end-to-end needs no GitHub account."""

    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    def get_repo(self, full_name: str) -> RecordingGitHub:
        return self

    def get_pulls(self, **kw: object) -> list[object]:
        return []

    def create_pull(self, **kw: object) -> object:
        self.created.append(kw)
        return type("PR", (), {"html_url": f"local://pull/{len(self.created)}"})()


@pytest.fixture
async def e2e_deps(
    e2e_settings: Settings, migrated_pg_url: str, redis_url: str
) -> AsyncIterator[Deps]:
    settings = e2e_settings.model_copy(
        update={"database_url": migrated_pg_url, "redis_url": redis_url}
    )
    deps = Deps.build(settings)
    deps.github = RecordingGitHub()
    try:
        yield deps
    finally:
        await deps.aclose()
