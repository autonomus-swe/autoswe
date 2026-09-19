"""Reaping what finished runs leave behind, and refusing to reap anything else.

Against a real database, because the whole safety condition is a query and a mocked one
would be a test of the mock. The dangerous failure is deleting the working directory of a
*live* run, and it would happen rarely enough to be very hard to reproduce — so the tests
below are mostly about what the collector must NOT touch.

The one that matters is `test_a_run_marked_failed_but_still_running_is_left_alone`.
`set_run_phase` writes `status="failed"` the moment the phase machine transitions, while
the run is still inside its loop holding that directory; only `finish_run` sets
`finished_at`. A collector keyed on status alone passes every other test in this file and
destroys live work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from contracts import Budget
from orchestrator import gc
from storage import repo as db

pytestmark = pytest.mark.integration

HOUR = 3600


async def a_run(s: AsyncSession, *, status: str | None = None, finished_ago_s: int | None = None):  # type: ignore[no-untyped-def]
    """A run row, optionally finished. Returns its id."""
    run_id = await db.create_run(
        s,
        repo_url="https://github.com/a/b",
        base_branch="main",
        goal="g",
        budget=Budget(),
        provider="openai_compat",
    )
    if status is not None:
        await db.set_run_phase(s, run_id, "failed", status)
    if finished_ago_s is not None:
        await db.finish_run(s, run_id, status=status or "failed")
        from sqlalchemy import update

        from storage.models import RunRow

        await s.execute(
            update(RunRow)
            .where(RunRow.id == run_id)
            .values(finished_at=datetime.now(UTC) - timedelta(seconds=finished_ago_s))
        )
    await s.commit()
    return run_id


def worktree(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "file.py").write_text("x = 1\n")
    return path


# ---- what must not be touched ----------------------------------------------------------------


async def test_a_run_marked_failed_but_still_running_is_left_alone(
    engine: AsyncEngine, db: AsyncSession, host_tmp: Path
) -> None:
    """The one that matters.

    `set_run_phase` writes a terminal *status* as soon as the phase machine transitions —
    the run is still in its loop with this directory open. Only `finish_run` sets
    `finished_at`. Reaping on status alone deletes live work.
    """
    run_id = await a_run(db, status="failed")  # status set, finished_at still NULL
    root = host_tmp / "wt"
    path = worktree(root, str(run_id))

    # Both graces at zero on purpose. With the orphan grace at its default this test passes
    # for the wrong reason — the directory is young — and keeps passing with the safety
    # condition deleted. Verified: removing the `finished_at IS NULL` clause from
    # `runs_unfinished` leaves this green until `orphan_grace_s=0` takes that escape away.
    swept = await gc.collect(
        engine,
        worktrees_dir=root,
        repos_dir=host_tmp / "none",
        worktree_ttl_s=0,
        orphan_grace_s=0,
    )

    assert path.is_dir(), "a run that has not called finish_run is still running"
    assert swept.skipped.get("worktree_run_alive") == 1, (
        "and it must be skipped for being alive, not for being young"
    )


async def test_a_freshly_finished_run_is_inside_its_grace(
    engine: AsyncEngine, db: AsyncSession, host_tmp: Path
) -> None:
    """The TTL is the window in which a human can still look at what a failed run left."""
    run_id = await a_run(db, status="failed", finished_ago_s=60)
    root = host_tmp / "wt"
    path = worktree(root, str(run_id))

    await gc.collect(engine, worktrees_dir=root, repos_dir=host_tmp / "none", worktree_ttl_s=HOUR)

    assert path.is_dir()


async def test_a_directory_with_no_run_at_all_gets_a_much_longer_grace(
    engine: AsyncEngine, host_tmp: Path
) -> None:
    """ "I cannot explain this directory" is a worse reason to delete something than "this
    is old"."""
    root = host_tmp / "wt"
    path = worktree(root, "not-a-run-id")

    await gc.collect(
        engine,
        worktrees_dir=root,
        repos_dir=host_tmp / "none",
        worktree_ttl_s=0,
        orphan_grace_s=HOUR,
    )

    assert path.is_dir()


async def test_a_running_container_is_skipped_whatever_its_row_says(
    engine: AsyncEngine, db: AsyncSession, host_tmp: Path
) -> None:
    """A row can be wrong about a process; a process cannot be wrong about itself."""
    run_id = await a_run(db, status="failed", finished_ago_s=2 * HOUR)

    class Container:
        name = "run-x"
        status = "running"
        labels: ClassVar[dict[str, str]] = {"autoswe.run_id": str(run_id)}
        removed = False

        def remove(self, force: bool = False) -> None:
            Container.removed = True

    class Client:
        containers = type("C", (), {"list": staticmethod(lambda **kw: [Container()])})()

    swept = await gc.collect(
        engine,
        worktrees_dir=host_tmp / "none",
        repos_dir=host_tmp / "none",
        client=Client(),
        sandbox_ttl_s=0,
    )

    assert not Container.removed
    assert swept.skipped.get("container_still_running") == 1


# ---- what must be ------------------------------------------------------------------------------


async def test_a_run_that_finished_long_ago_is_reaped(
    engine: AsyncEngine, db: AsyncSession, host_tmp: Path
) -> None:
    run_id = await a_run(db, status="failed", finished_ago_s=2 * HOUR)
    root = host_tmp / "wt"
    path = worktree(root, str(run_id))

    swept = await gc.collect(
        engine, worktrees_dir=root, repos_dir=host_tmp / "none", worktree_ttl_s=HOUR
    )

    assert not path.exists()
    assert swept.worktrees == 1


async def test_an_old_orphan_is_reaped_once_past_its_longer_grace(
    engine: AsyncEngine, host_tmp: Path
) -> None:
    root = host_tmp / "wt"
    path = worktree(root, "abandoned")

    swept = await gc.collect(
        engine,
        worktrees_dir=root,
        repos_dir=host_tmp / "none",
        worktree_ttl_s=0,
        orphan_grace_s=0,
    )

    assert not path.exists() and swept.worktrees == 1


async def test_an_exited_container_of_a_finished_run_is_removed(
    engine: AsyncEngine, db: AsyncSession, host_tmp: Path
) -> None:
    run_id = await a_run(db, status="done", finished_ago_s=2 * HOUR)
    removed: list[str] = []

    class Container:
        name = "run-x"
        status = "exited"
        labels: ClassVar[dict[str, str]] = {"autoswe.run_id": str(run_id)}

        def remove(self, force: bool = False) -> None:
            removed.append(self.name)

    class Client:
        containers = type("C", (), {"list": staticmethod(lambda **kw: [Container()])})()

    swept = await gc.collect(
        engine,
        worktrees_dir=host_tmp / "none",
        repos_dir=host_tmp / "none",
        client=Client(),
        sandbox_ttl_s=HOUR,
    )

    assert removed == ["run-x"] and swept.containers == 1


# ---- it never takes the worker down ------------------------------------------------------------


async def test_an_unreachable_docker_does_not_stop_the_worktree_sweep(
    engine: AsyncEngine, db: AsyncSession, host_tmp: Path
) -> None:
    """A worker without Docker still has directories to reap."""
    run_id = await a_run(db, status="failed", finished_ago_s=2 * HOUR)
    root = host_tmp / "wt"
    path = worktree(root, str(run_id))

    class Broken:
        @property
        def containers(self) -> object:
            raise RuntimeError("no docker here")

    swept = await gc.collect(
        engine,
        worktrees_dir=root,
        repos_dir=host_tmp / "none",
        client=Broken(),
        worktree_ttl_s=HOUR,
    )

    assert not path.exists() and swept.worktrees == 1


async def test_a_missing_directory_is_not_an_error(engine: AsyncEngine, host_tmp: Path) -> None:
    """A fresh machine has neither directory yet."""
    swept = await gc.collect(
        engine, worktrees_dir=host_tmp / "nope", repos_dir=host_tmp / "also-nope"
    )

    assert swept.removed == 0
