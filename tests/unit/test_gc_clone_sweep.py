"""The clone sweep: the one GC pass that can destroy work it cannot see.

`tests/integration/test_gc.py` covers the container and worktree sweeps thoroughly — nine
tests, including the two that matter most ("a row can be wrong about a process; a process
cannot be wrong about itself", and the longer grace for a directory nobody can explain).

**`sweep_clones` appears in none of them.** Four mutations survived the whole suite:

| mutation | consequence |
|---|---|
| the attached-worktree guard → never | **a clone with live worktrees is deleted** |
| the young-clone guard → never | a clone fetched into minutes ago is deleted |
| the unreadable-clone skip → fall through | a clone is deleted *because* it could not be read |
| `ttl_days * 24 * 3600` → `ttl_days` | the TTL becomes seconds, so everything is ancient |

The first is the serious one and the module's own docstring says why: a clone is "the
expensive thing to rebuild — a fresh one of a large repository is minutes — and it is shared
by every run against that repository, so age alone is not enough". Deleting one with
worktrees attached takes the git objects out from under every live run on that repository at
once. The worktree directories survive and every git command inside them fails.

The third is the shape worth noticing: an error reading the state becomes permission to
delete. That is the opposite of what an unreadable thing deserves, and it is one line.

These are unit tests because `sweep_clones` touches no database — it takes a root, a TTL, a
clock and a counter. Real `git` repositories in `tmp_path`, because the guard's whole
question is what `git worktree list` says, and a fake would be asserting the fake.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator import gc

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin",
}


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=GIT_ENV
    )
    return done.stdout


def bare_clone_with_a_commit(root: Path, name: str) -> Path:
    """A bare repository with one commit, the shape `ensure_bare_clone` leaves behind.

    The source it is cloned from lives *outside* `root`, deliberately. Put it inside and
    `_scan` counts it as another clone, and every assertion about how many were swept is
    then counting the fixture — which is how the first version of this file reported two
    skips where it expected one.
    """
    source = root.parent / "sources" / name
    source.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=source)
    (source / "f.txt").write_text("x\n")
    git("add", "-A", cwd=source)
    git("commit", "-qm", "one", cwd=source)

    bare = root / name
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(bare)],
        check=True,
        capture_output=True,
        env=GIT_ENV,
    )
    return bare


def age(path: Path, *, days: float) -> None:
    """Backdate a directory's mtime, which is what the TTL is measured against."""
    when = NOW - timedelta(days=days)
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


async def sweep(root: Path, *, ttl_days: int = 7) -> gc.Swept:
    swept = gc.Swept()
    await gc.sweep_clones(root, ttl_days, NOW, swept)
    return swept


# ---- the guard that matters ---------------------------------------------------------------


async def test_a_clone_with_a_worktree_attached_is_never_deleted(tmp_path: Path) -> None:
    """The serious one.

    A bare clone is shared by every run against that repository. Deleting one that still has
    worktrees attached takes the git objects out from under all of them at once — the
    worktree directories survive, and every git command inside them fails afterwards with
    something that names neither the clone nor the collector.

    Aged well past the TTL on purpose: the guard has to be what saves it, not its mtime.
    """
    clones = tmp_path / "clones"
    bare = bare_clone_with_a_commit(clones, "in-use.git")
    worktree = tmp_path / "worktrees" / "run-1"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", str(worktree), "main", cwd=bare)
    age(bare, days=400)

    swept = await sweep(clones)

    assert bare.is_dir(), "a clone with a live worktree was deleted"
    assert swept.clones == 0
    assert swept.skipped.get("clone_has_worktrees") == 1, "and it says why it was spared"


async def test_a_clone_with_no_worktrees_left_is_deleted_once_it_is_old(
    tmp_path: Path,
) -> None:
    """The counterweight. Without it the guard could become "never delete anything", which
    would satisfy the test above while letting the disk fill up — the thing the collector
    exists to prevent.
    """
    clones = tmp_path / "clones"
    bare = bare_clone_with_a_commit(clones, "abandoned.git")
    age(bare, days=400)

    swept = await sweep(clones)

    assert not bare.exists(), "an old clone with nothing attached should be collected"
    assert swept.clones == 1


async def test_a_worktree_that_was_removed_releases_the_clone(tmp_path: Path) -> None:
    """The lifecycle, end to end: attached and spared, detached and collected.

    This is what makes the guard a fact about the clone rather than about the moment the
    collector happened to look.
    """
    clones = tmp_path / "clones"
    bare = bare_clone_with_a_commit(clones, "cycles.git")
    worktree = tmp_path / "worktrees" / "run-1"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", str(worktree), "main", cwd=bare)
    age(bare, days=400)

    assert (await sweep(clones)).clones == 0, "spared while attached"

    git("worktree", "remove", "--force", str(worktree), cwd=bare)
    age(bare, days=400)  # `worktree remove` touches the clone, so re-age it

    assert (await sweep(clones)).clones == 1, "collected once nothing is attached"
    assert not bare.exists()


# ---- the age ------------------------------------------------------------------------------


async def test_a_young_clone_is_left_alone_however_little_is_attached(
    tmp_path: Path,
) -> None:
    """A clone fetched into an hour ago is a clone the next run will reuse. Rebuilding one
    is minutes, and the window between "no worktrees right now" and "a run starting" is
    exactly where this would bite."""
    clones = tmp_path / "clones"
    bare = bare_clone_with_a_commit(clones, "fresh.git")
    age(bare, days=1)

    swept = await sweep(clones, ttl_days=7)

    assert bare.is_dir()
    assert swept.skipped.get("clone_young") == 1


async def test_the_ttl_is_days_rather_than_seconds(tmp_path: Path) -> None:
    """`cutoff_s = ttl_days * 24 * 3600`. Without the conversion a TTL of 7 means seconds,
    so every clone older than seven seconds is ancient and the collector deletes the lot on
    its first pass.

    Asserted by straddling the boundary rather than by reading the arithmetic: six days old
    against a seven-day TTL must survive, eight days must not.
    """
    clones = tmp_path / "clones"
    young = bare_clone_with_a_commit(clones, "six-days.git")
    old = bare_clone_with_a_commit(clones, "eight-days.git")
    age(young, days=6)
    age(old, days=8)

    swept = await sweep(clones, ttl_days=7)

    assert young.is_dir(), "six days is inside a seven-day TTL"
    assert not old.exists(), "eight days is outside it"
    assert swept.clones == 1


# ---- what an unreadable clone deserves ----------------------------------------------------


async def test_a_clone_whose_state_cannot_be_read_is_skipped_not_deleted(
    tmp_path: Path,
) -> None:
    """An error reading the state must not become permission to delete.

    `git worktree list` failing means the collector does not know whether anything is
    attached — which is the one circumstance in which deleting is least defensible. Falling
    through to the removal survived the suite, and it is a single `continue`.

    The clone here is a directory that is not a git repository at all, which is what a
    half-finished or corrupted clone looks like on disk.
    """
    clones = tmp_path / "clones"
    broken = clones / "not-a-repo.git"
    broken.mkdir(parents=True)
    (broken / "a-file").write_text("this is not a git repository\n")
    age(broken, days=400)

    swept = await sweep(clones)

    assert broken.is_dir(), "a clone that could not be read was deleted because of it"
    assert swept.clones == 0
    assert swept.skipped.get("clone_unreadable") == 1


# ---- the ordinary shape -------------------------------------------------------------------


async def test_a_missing_clones_directory_is_not_an_error(tmp_path: Path) -> None:
    """The collector runs on a worker that may never have cloned anything."""
    swept = await sweep(tmp_path / "never-existed")
    assert swept.clones == 0 and swept.skipped == {}


async def test_a_file_among_the_clones_is_ignored_rather_than_deleted(
    tmp_path: Path,
) -> None:
    """`_scan` yields directories only. A stray file is somebody else's, and the collector
    has no business with it either way."""
    clones = tmp_path / "clones"
    clones.mkdir()
    stray = clones / "notes.txt"
    stray.write_text("not a clone\n")
    os.utime(stray, (time.time() - 400 * 86400,) * 2)

    swept = await sweep(clones)

    assert stray.exists()
    assert swept.clones == 0
