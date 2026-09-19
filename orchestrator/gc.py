"""Reaping what a finished run leaves behind.

`teardown` removes a worktree only when the run pushed — a failed run's tree is the
evidence somebody needs to read — and that is deliberate. What was missing is anything to
remove it *later*. Measured on a development machine after Phase 5: 42 worktrees at
332 MB, 55 bare clones, and 26 exited containers still labelled `autoswe.run_id`.

## The safety condition is the whole design

A collector that deletes the working directory of a *running* run destroys live work, and
would do it rarely enough to be hard to reproduce. So the rule is narrow and everything
ambiguous is left alone:

**A run is reapable only when `status` is terminal AND `finished_at` is set AND
`finished_at` is older than the TTL.**

The middle clause is the one that matters. `set_run_phase` writes `status="done"` or
`"failed"` from `status_for(phase)` the moment the phase machine transitions — while the
run is still inside its loop, holding its worktree and its container. Only `finish_run`
sets `finished_at`, and it is the last thing a run does. So a run can be terminal by status
and very much alive, and a collector keyed on status alone deletes the directory out from
under it.

Everything else follows the same instinct. A directory whose run cannot be found is given a
much longer grace, because "I cannot explain this" is a worse reason to delete something
than "this is old". A probe that raises is a skip, never a delete. A container that is
still running is skipped whatever its row says, because a row can be wrong about a process
and a process cannot be wrong about itself.

## It reports what it did not do

Every skip is counted and logged with its reason. A collector that quietly reclaims nothing
looks exactly like one that had nothing to reclaim, and the disk that keeps growing is the
only difference.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from observability.logging import get_logger
from repo.gitcmd import git
from storage import repo as db
from storage.db import session

log = get_logger(__name__)

# One pass never removes more than this of anything, so a first run on a machine that has
# been accumulating for months cannot spend ten minutes in `rm -rf` while runs wait.
BATCH = 200


@dataclass
class Swept:
    """What one pass did, and what it deliberately did not."""

    containers: int = 0
    worktrees: int = 0
    clones: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def removed(self) -> int:
        return self.containers + self.worktrees + self.clones


def _scan(root: Path) -> list[tuple[Path, datetime]]:
    """`(directory, mtime)` for each child, oldest first. Synchronous by design.

    Every filesystem call in this module happens inside one of these helpers and reaches
    the event loop only through `asyncio.to_thread`. That is not lint appeasement: the
    collector runs on the worker alongside live runs, and `rmtree` on a 300 MB worktree
    would stall every one of them for as long as it took.
    """
    if not root.is_dir():
        return []
    out: list[tuple[Path, datetime]] = []
    for path in root.iterdir():
        try:
            if path.is_dir():
                out.append((path, datetime.fromtimestamp(path.stat().st_mtime, UTC)))
        except OSError:
            continue
    return sorted(out, key=lambda pair: pair[1])[:BATCH]


def _age_s(when: datetime | None, now: datetime) -> float:
    """Seconds since `when`, or -1 when it is unknown — which never passes a TTL test."""
    if when is None:
        return -1.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now - when).total_seconds()


async def _reapable(engine: Any, now: datetime, ttl_s: int) -> tuple[set[str], set[str]]:
    """`(run ids safe to reap, run ids that must be left alone)`.

    Both sets are returned because "not in the reapable set" and "known to be alive" are
    different facts, and the caller needs the second to decide what to do about a directory
    it cannot explain.
    """
    cutoff = now - timedelta(seconds=ttl_s)
    async with session(engine) as s:
        finished = await db.runs_finished_before(s, cutoff, limit=BATCH)
        alive = await db.runs_unfinished(s)
    return {str(r.id) for r in finished}, {str(r.id) for r in alive}


async def sweep_containers(client: Any, reapable: set[str], alive: set[str], swept: Swept) -> None:
    """Containers whose run is over. Labelled `autoswe.run_id`, so nothing else is touched."""
    try:
        containers = client.containers.list(all=True, filters={"label": "autoswe.run_id"})
    except Exception as e:
        log.warning("gc_containers_unreadable", error=f"{type(e).__name__}: {e}")
        return
    for container in containers:
        run_id = container.labels.get("autoswe.run_id", "")
        if run_id in alive:
            swept.skip("container_run_alive")
            continue
        if run_id not in reapable:
            swept.skip("container_run_unknown")
            continue
        # A row can be wrong about a process; a process cannot be wrong about itself.
        if container.status == "running":
            swept.skip("container_still_running")
            continue
        try:
            container.remove(force=True)
            swept.containers += 1
        except Exception as e:
            log.warning("gc_container_failed", container=container.name, error=str(e)[:200])


async def sweep_worktrees(
    root: Path,
    reapable: set[str],
    alive: set[str],
    now: datetime,
    orphan_grace_s: int,
    swept: Swept,
) -> None:
    """Worktrees of finished runs, and long-abandoned directories with no run at all."""
    for path, mtime in await asyncio.to_thread(_scan, root):
        name = path.name
        if name in alive:
            swept.skip("worktree_run_alive")
            continue
        if name not in reapable:
            # No run row, or one that is not finished. Give it a much longer grace: a
            # directory nobody can explain is a worse thing to delete on a timer than one
            # whose run is on record as over.
            if _age_s(mtime, now) < orphan_grace_s:
                swept.skip("worktree_orphan_young")
                continue
        await _remove(path, swept, "worktrees")


async def _remove(path: Path, swept: Swept, kind: str) -> None:
    """Delete a tree, off the loop, counting the failure rather than raising it."""
    try:
        await asyncio.to_thread(shutil.rmtree, path)
        setattr(swept, kind, getattr(swept, kind) + 1)
    except OSError as e:
        log.warning("gc_remove_failed", path=str(path), error=str(e)[:200])
        swept.skip(f"{kind}_remove_failed")


async def sweep_clones(root: Path, ttl_days: int, now: datetime, swept: Swept) -> None:
    """Bare clones nothing has fetched into for a long time.

    The highest bar of the three. A clone is the expensive thing to rebuild — a fresh one
    of a large repository is minutes — and it is shared by every run against that
    repository, so age alone is not enough: a clone with a worktree still attached is in
    use whatever its mtime says.
    """
    cutoff_s = ttl_days * 24 * 3600
    for path, mtime in await asyncio.to_thread(_scan, root):
        if _age_s(mtime, now) < cutoff_s:
            swept.skip("clone_young")
            continue
        try:
            attached = await git("worktree", "list", "--porcelain", cwd=path)
        except Exception:
            swept.skip("clone_unreadable")
            continue
        if attached.count("worktree ") > 1:  # the bare repo itself is always the first
            swept.skip("clone_has_worktrees")
            continue
        await _remove(path, swept, "clones")


async def collect(
    engine: Any,
    *,
    worktrees_dir: Path,
    repos_dir: Path,
    client: Any | None = None,
    sandbox_ttl_s: int = 3600,
    worktree_ttl_s: int = 3600,
    orphan_grace_s: int = 24 * 3600,
    bare_clone_ttl_days: int = 14,
    now: datetime | None = None,
) -> Swept:
    """One pass. Never raises: a collector that can fail a worker is worse than a full disk.

    `now` is a parameter so a test can age things without sleeping, and `client` so one can
    run without Docker.
    """
    moment = now or datetime.now(UTC)
    swept = Swept()
    try:
        container_reapable, alive = await _reapable(engine, moment, sandbox_ttl_s)
        worktree_reapable, _ = await _reapable(engine, moment, worktree_ttl_s)

        if client is not None:
            await sweep_containers(client, container_reapable, alive, swept)
        await sweep_worktrees(
            worktrees_dir, worktree_reapable, alive, moment, orphan_grace_s, swept
        )
        await sweep_clones(repos_dir, bare_clone_ttl_days, moment, swept)
    except Exception as e:
        log.warning("gc_failed", error=f"{type(e).__name__}: {e}")
        return swept

    # Logged even when it removed nothing, with the reasons. A collector that quietly
    # reclaims nothing looks exactly like one with nothing to reclaim, and the disk that
    # keeps growing is the only way to tell them apart.
    log.info(
        "gc_swept",
        containers=swept.containers,
        worktrees=swept.worktrees,
        clones=swept.clones,
        skipped=swept.skipped,
    )
    return swept


async def run_gc(ctx: dict[str, Any]) -> str:
    """The arq cron entry point."""
    from core.settings import get_settings

    settings = get_settings()
    if not settings.gc_enabled:
        return "disabled"

    from storage.db import make_engine

    engine = make_engine(settings.database_url, pool_size=2, max_overflow=0)
    client = None
    try:
        import docker

        client = docker.from_env()
    except Exception as e:  # a worker without Docker still has worktrees to reap
        log.info("gc_without_docker", error=f"{type(e).__name__}: {e}")
    try:
        swept = await collect(
            engine,
            worktrees_dir=settings.worktrees_dir,
            repos_dir=settings.repos_dir,
            client=client,
            sandbox_ttl_s=settings.sandbox_ttl_s,
            worktree_ttl_s=settings.worktree_ttl_s,
            orphan_grace_s=settings.orphan_grace_s,
            bare_clone_ttl_days=settings.bare_clone_ttl_days,
        )
    finally:
        await engine.dispose()
    return f"{swept.removed} removed"
