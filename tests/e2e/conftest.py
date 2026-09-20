"""End-to-end fixtures. These call a real model and cost money, so they are opt-in:
they run only when LLM_API_KEY is set and are selected with `-m e2e`."""

from __future__ import annotations

import os
import shutil
import signal
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.settings import Settings, load_settings
from orchestrator.deps import Deps
from repo.gitcmd import git


def _raise_on_terminate(signum: int, frame: object) -> None:
    """Turn a termination signal into an exception so `finally` blocks still run.

    An e2e run here is hours, so it is far more likely to be stopped than to end on its
    own — a CI step timeout, `timeout(1)`, Ctrl-C's neighbour, an operator with a deadline.
    Python's default SIGTERM handling tears the process down without unwinding the stack,
    which means every `finally` is skipped.

    That is expensive in exactly this suite. `test_m5_cache` computes its report in a
    `finally` precisely so a failed run still records its numbers, but the report reads the
    `llm_calls` ledger out of a testcontainer Postgres that dies with the process. So a
    terminated run does not lose the *last* measurement — it loses all of them, and the
    evidence a Phase 5 criterion rests on has to be gathered again from scratch.

    Raising `KeyboardInterrupt` rather than `SystemExit`: pytest treats it as a session
    interrupt and still runs teardown, and it is the exception the stdlib already uses for
    "someone asked this to stop".
    """
    raise KeyboardInterrupt(f"terminated by signal {signum}")


@contextmanager
def unwinding_on_termination() -> Iterator[None]:
    """Install that handler, then put back whatever was there.

    Restoring matters: leaving it installed would change how the whole pytest session
    dies, including for tests that never asked for this.

    A plain context manager rather than only a fixture, so the behaviour can be tested
    directly — pytest refuses to let a fixture be called outside a test, and a guard this
    small is worth pinning exactly.
    """
    try:
        previous = signal.signal(signal.SIGTERM, _raise_on_terminate)
    except ValueError:  # not the main thread; nothing to install
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.fixture(autouse=True)
def _unwind_on_termination() -> Iterator[None]:
    with unwinding_on_termination():
        yield


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
def chaos_repo(e2e_settings: Settings) -> Iterator[Path]:
    """The chaos fixture repository, one branch per scenario. See tests/e2e/chaos.py."""
    import asyncio

    from tests.e2e.chaos import materialise

    base = Path(os.environ.get("AUTOSWE_TEST_TMP", Path.home() / ".autoswe" / "tmp"))
    root = base / f"chaos-{uuid.uuid4().hex[:8]}"
    try:
        asyncio.run(materialise(root / "origin"))
        yield root / "origin"
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


@pytest.fixture
def scale_repo(e2e_settings: Settings) -> Iterator[Path]:
    """A throwaway clone of a large repository named by `AUTOSWE_SCALE_REPO`.

    Cloned, never used in place. The path points at a working checkout somebody keeps for
    other purposes, and a run creates branches, writes files and resets — doing that to
    the original would be destroying someone's work to measure a cache hit rate. A local
    `git clone` hardlinks its objects, so even a 7 000-file repository costs little.

    Skipped rather than failed when the variable is unset: this is the one end-to-end test
    that needs a large repository on disk, and the rest of the suite should not depend on
    anyone having cloned Django.
    """
    source = os.environ.get("AUTOSWE_SCALE_REPO")
    if not source:
        pytest.skip("AUTOSWE_SCALE_REPO is not set; the scale run is opt-in")
    origin = Path(source).expanduser().resolve()
    if not (origin / ".git").is_dir():
        pytest.skip(f"{origin} is not a git checkout")

    base = Path(os.environ.get("AUTOSWE_TEST_TMP", Path.home() / ".autoswe" / "tmp"))
    root = base / f"scale-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    try:
        import asyncio

        async def clone() -> None:
            # `--no-hardlinks` is deliberately *not* passed: hardlinked objects are what
            # make this cheap, and nothing here writes to the object store of the source.
            await git("clone", "-q", "--local", str(origin), str(root / "origin"))

        asyncio.run(clone())
        yield root / "origin"
    finally:
        shutil.rmtree(root, ignore_errors=True)


class RecordingPR:
    """The pull request PyGithub hands back, with the one method `open_pr` calls on it.

    `add_to_labels` is applied after creation, because `create_pull` takes no labels, and
    `open_pr` treats a failure there as best-effort — it logs and carries on rather than
    losing a pull request over a label that does not exist in the repository.

    That is the right trade and it is why this method has to exist here. Without it the
    stub raises `AttributeError`, `open_pr` swallows it, and every end-to-end run emits a
    `pr_labels_failed` warning that looks like a product defect and is not. Worse, the
    suite cannot then tell "labels were applied" from "labelling failed and was
    swallowed" — the two outcomes are identical from outside, which is exactly the
    distinction the integration stub records labels in order to preserve.
    """

    def __init__(self, url: str) -> None:
        self.html_url = url
        self.labels: list[str] = []

    def add_to_labels(self, *labels: str) -> None:
        self.labels.extend(labels)


class RecordingGitHub:
    """Stands in for PyGithub so the local end-to-end needs no GitHub account."""

    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.pulls: list[RecordingPR] = []

    def get_repo(self, full_name: str) -> RecordingGitHub:
        return self

    def get_pulls(self, **kw: object) -> list[object]:
        return []

    def create_pull(self, **kw: object) -> RecordingPR:
        self.created.append(kw)
        pr = RecordingPR(f"local://pull/{len(self.created) + 1}")
        self.pulls.append(pr)
        return pr

    @property
    def labelled(self) -> list[str]:
        """Every label applied across every pull request this stub opened."""
        return [label for pr in self.pulls for label in pr.labels]


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
