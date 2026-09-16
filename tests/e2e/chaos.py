"""Build the chaos fixture repository on disk, one branch per scenario.

A run clones from a path as happily as from a URL, so the scenarios need no GitHub account
and no network: `base_branch` selects which one a run gets, exactly as it would against a
real remote.

See ``tests/fixtures/bugs_repo/README.md`` for what each branch is for.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from repo.gitcmd import git

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "bugs_repo"
BASE = FIXTURE / "base"
SCENARIOS = FIXTURE / "scenarios"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", ".pytest_cache", ".autoswe")


@dataclass(frozen=True)
class Scenario:
    """One branch, and what the run against it is asked to do."""

    branch: str
    goal: str


# The goals name the test file on purpose. A task whose selector is known is the case the
# baseline filter is safe in — see `docs/ARCHITECTURE.md` on what is never excused — and
# naming it is also simply what a person would do.
A_OFF_BY_ONE = Scenario(
    "a-off-by-one",
    "tests/test_pages.py is failing: paginate() drops the last partial page. Fix "
    "chaos/pages.py so the whole file passes. Do not change the tests.",
)
B_MISSING_IMPORT = Scenario(
    "b-missing-import",
    "tests/test_stamp.py cannot even be collected. Fix chaos/stamp.py so it passes. "
    "Do not change the tests.",
)
C_IMPOSSIBLE = Scenario(
    "c-impossible",
    "Make tests/test_impossible.py pass by changing chaos/pages.py. Do not change the "
    "tests under any circumstances.",
)
D_NETWORK = Scenario(
    "d-network",
    "tests/test_fetch.py is failing. Fix it by changing chaos/fetch.py. Do not change the tests.",
)
E_BASELINE = Scenario(
    "e-baseline",
    "tests/test_pages.py is failing: paginate() drops the last partial page. Fix "
    "chaos/pages.py so tests/test_pages.py passes. Do not change the tests.",
)
ALL = (A_OFF_BY_ONE, B_MISSING_IMPORT, C_IMPOSSIBLE, D_NETWORK, E_BASELINE)


def branches() -> list[str]:
    """Scenario directory names, which are also the branch names."""
    return sorted(p.name for p in SCENARIOS.iterdir() if p.is_dir())


async def _copy(src: Path, dst: Path) -> None:
    await asyncio.to_thread(shutil.copytree, src, dst, dirs_exist_ok=True, ignore=IGNORE)


async def materialise(root: Path) -> Path:
    """Create the repository at ``root`` with ``main`` plus one branch per scenario.

    Every branch is a commit on top of the green ``main``, so the diff a run sees is the
    scenario and nothing else.
    """
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    await _copy(BASE, root)
    await git("init", "-q", "-b", "main", cwd=root)
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", "chore: the library as it should work", cwd=root)

    for branch in branches():
        await git("checkout", "-q", "main", cwd=root)
        await git("checkout", "-q", "-b", branch, cwd=root)
        await _copy(SCENARIOS / branch, root)
        await git("add", "-A", cwd=root)
        await git("commit", "-q", "-m", f"chore({branch}): the scenario", cwd=root)
    # Leave it on main: a clone of a repository checked out on a scenario branch would
    # make that scenario the default, which is a confusing thing to debug later.
    await git("checkout", "-q", "main", cwd=root)
    return root
