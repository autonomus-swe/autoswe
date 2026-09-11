"""Continue a run that stopped, from wherever it got to.

Resume re-attaches infrastructure rather than rebuilding it: an existing bare clone,
worktree and container are reused, so a crash costs the work of one node and not the
whole run.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

from core.errors import AutosweError, SandboxError
from observability.logging import get_logger
from orchestrator.checkpoint import load_latest
from orchestrator.deps import Deps
from orchestrator.nodes import LOCK_TTL_S, RunResources, install_command
from orchestrator.state import DEFAULT_TEST_COMMAND, Phase, RunState
from repo import worktree as wt
from repo.clone import ensure_bare_clone, repo_key
from storage import repo as db
from storage.db import session

log = get_logger(__name__)


async def initial_state(engine: Any, run_id: UUID) -> RunState:
    """Build the starting state from the runs row, for a run with no checkpoint."""
    async with session(engine) as s:
        row = await db.get_run(s, run_id)
    if row is None:
        raise AutosweError(f"run {run_id} not found")
    return RunState(
        run_id=row.id,
        goal=row.goal,
        repo_url=row.repo_url,
        base_branch=row.base_branch,
        work_branch=row.work_branch,
        unattended=bool(row.unattended),
        phase=Phase.SETUP,
        test_command=DEFAULT_TEST_COMMAND,
    )


async def load_state(deps: Deps, run_id: UUID) -> tuple[RunState, bool]:
    """``(state, resumed)`` — the checkpoint when there is one, else a fresh state."""
    state = await load_latest(deps.engine, run_id)
    if state is not None:
        return state, True
    return await initial_state(deps.engine, run_id), False


async def reattach(state: RunState, deps: Deps, res: RunResources, worker_id: str) -> None:
    """Take back the lock, worktree and container this run was using.

    Only called for a resumed run. SETUP builds these itself on a first attempt.
    """
    res.lock_key = f"lock:repo:{repo_key(state.repo_url)}:{state.base_branch}"
    res.lock_owner = worker_id
    if not await deps.bus.acquire_lock(res.lock_key, worker_id, LOCK_TTL_S):
        holder = await deps.bus.lock_owner(res.lock_key)
        if holder != worker_id:  # another worker already took over this run
            res.lock_key = None
            raise AutosweError(f"cannot resume: {state.repo_url} is held by {holder}")

    token = deps.settings.github_token.get_secret_value() if deps.settings.github_token else None
    bare = await ensure_bare_clone(state.repo_url, deps.repos_dir(), token)

    path = deps.worktrees_dir() / str(state.run_id)
    if path.is_dir():
        res.worktree = wt.Worktree(
            path=path, branch=state.work_branch, bare=bare, run_id=str(state.run_id)
        )
        log.info("resume_worktree_reused", path=str(path))
    else:
        res.worktree = await wt.create(
            bare, deps.worktrees_dir(), state.run_id, state.base_sha or state.base_branch
        )
        log.info("resume_worktree_recreated", path=str(path))

    sandbox = deps.sandbox_factory(state.run_id, res.worktree.path)
    res.sandbox = sandbox
    with contextlib.suppress(SandboxError):
        await sandbox.stop(remove=True)  # a container from the dead attempt cannot be trusted
    await sandbox.start()
    await sandbox.connect_install_network()
    await sandbox.exec(install_command(res.worktree.path, state.facts), timeout_s=900)
    await sandbox.disconnect_network()
    if await sandbox.has_network():
        raise SandboxError("sandbox still has network access after resume")
    log.info("resume_reattached", run_id=str(state.run_id), phase=state.phase.value)
