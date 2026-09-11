"""Killing a worker outright must cost one node, not the whole run.

The kill is a real SIGKILL to a real child process. An in-process exception would not
prove anything: ``runner.run`` catches it, marks the run failed and tears the sandbox
down, which is precisely what a crash does not do.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from contracts import Budget
from core.settings import load_settings
from orchestrator.deps import Deps, docker_sandbox_factory
from orchestrator.nodes import RunResources
from orchestrator.resume import load_state, reattach
from orchestrator.runner import run
from orchestrator.state import Phase
from orchestrator.worker import worker_id
from repo.clone import repo_key
from storage import repo as db
from storage.db import make_engine, session
from storage.redis import RedisBus
from tests.integration.test_full_run import (
    GOAL,
    IMAGE,
    ScriptedAgents,
    StubGitHub,
    requires_docker,
)

pytestmark = pytest.mark.integration


class StallsInCode(ScriptedAgents):
    """Plays every agent up to CODE, then hangs so the kill lands at a known phase."""

    async def _complete(self, **kw: Any) -> Any:
        offered = {t["function"]["name"] for t in kw.get("tools", [])}
        if "submit_result" in offered:
            await asyncio.sleep(3600)
        return await super()._complete(**kw)


def _settings(pg_url: str, redis_url: str, host_tmp: str) -> Any:
    return load_settings(env_file=None).model_copy(
        update={
            "database_url": pg_url,
            "redis_url": redis_url,
            "worktrees_dir": Path(host_tmp) / "worktrees",
            "repos_dir": Path(host_tmp) / "repos",
            "sandbox_image": IMAGE,
        }
    )


def _deps(pg_url: str, redis_url: str, host_tmp: str, provider: Any) -> Deps:
    settings = _settings(pg_url, redis_url, host_tmp)
    return Deps(
        settings=settings,
        provider=provider,
        engine=make_engine(pg_url, poolclass=NullPool),
        bus=RedisBus(redis_url),
        sandbox_factory=docker_sandbox_factory(settings),
        github=StubGitHub(),
    )


async def _child_main(pg_url: str, redis_url: str, host_tmp: str, run_id: str) -> None:
    deps = _deps(pg_url, redis_url, host_tmp, StallsInCode())
    state, _ = await load_state(deps, UUID(run_id))
    await run(state, deps)


def _child(pg_url: str, redis_url: str, host_tmp: str, run_id: str) -> None:
    """Entry point for the spawned worker. Must be importable at module level."""
    asyncio.run(_child_main(pg_url, redis_url, host_tmp, run_id))


async def _phases_checkpointed(engine: Any, run_id: UUID) -> list[str]:
    async with session(engine) as s:
        rows = await s.execute(
            text("select phase from checkpoints where run_id = :r order by seq"),
            {"r": str(run_id)},
        )
    return [r[0] for r in rows]


async def test_a_second_worker_will_not_resume_a_run_another_worker_holds(
    migrated_pg_url: str, redis_url: str, host_tmp: Path
) -> None:
    """Two workers retrying the same job after a crash must not both run it.

    ``reattach`` takes the lock before it touches a clone or a container, so this needs
    no Docker: the refusal happens first or the exclusion is not worth much.
    """
    from core.errors import AutosweError

    deps = _deps(migrated_pg_url, redis_url, str(host_tmp), ScriptedAgents())
    await deps.bus.r.flushdb()
    async with session(deps.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url="https://github.com/acme/demo",
            base_branch="main",
            goal=GOAL,
            budget=Budget(),
        )
    try:
        state, _ = await load_state(deps, run_id)
        key = f"lock:repo:{repo_key(state.repo_url)}:{state.base_branch}"
        assert await deps.bus.acquire_lock(key, "worker-still-alive", ttl_s=60)

        with pytest.raises(AutosweError, match="worker-still-alive"):
            await reattach(state, deps, RunResources(), "worker-taking-over")

        # the live worker keeps the lock; the loser must not have stolen or dropped it
        assert await deps.bus.lock_owner(key) == "worker-still-alive"

        # once the lease lapses, the next worker is free to take the run over
        await deps.bus.r.pexpire(key, 50)
        await asyncio.sleep(0.3)
        assert await deps.bus.lock_owner(key) is None
        assert await deps.bus.acquire_lock(key, "worker-taking-over", ttl_s=60)
    finally:
        async with deps.engine.begin() as conn:
            await conn.execute(text("TRUNCATE runs CASCADE"))
        await deps.bus.close()
        await deps.engine.dispose()


@requires_docker
async def test_sigkill_after_plan_resumes_without_replanning(
    migrated_pg_url: str, redis_url: str, host_tmp: Path, origin_repo: Path
) -> None:
    engine = make_engine(migrated_pg_url, poolclass=NullPool)
    bus = RedisBus(redis_url)
    await bus.r.flushdb()
    async with session(engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(origin_repo),
            base_branch="main",
            goal=GOAL,
            budget=Budget(),
            unattended=True,
        )

    child = multiprocessing.get_context("spawn").Process(
        target=_child,
        args=(migrated_pg_url, redis_url, str(host_tmp), str(run_id)),
        daemon=True,
    )
    child.start()
    try:
        # wait until PLAN and DECOMPOSE are durable and the coder has started stalling
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if Phase.CODE.value in await _phases_checkpointed(engine, run_id):
                break
            if not child.is_alive():
                pytest.fail("worker exited before reaching CODE")
            await asyncio.sleep(1.0)
        else:
            pytest.fail("worker never reached CODE")

        before = await _phases_checkpointed(engine, run_id)
        assert before.count(Phase.PLAN.value) >= 1

        os.kill(child.pid or 0, signal.SIGKILL)
        child.join(timeout=30)
        assert child.exitcode == -signal.SIGKILL
    finally:
        if child.is_alive():  # never leave a worker behind if an assertion blew up
            os.kill(child.pid or 0, signal.SIGKILL)
            child.join(timeout=30)

    # a killed worker cannot tidy up: the run is still marked running, not failed
    async with session(engine) as s:
        row = await db.get_run(s, run_id)
    assert row is not None and row.status == "running", row.status if row else None

    # The dead worker still holds the repo lock; in production it falls to the lease TTL.
    # Expiring it by hand keeps this test about resume — the TTL has its own test below.
    held = [k async for k in bus.r.scan_iter("lock:repo:*")]
    assert held, "the crashed worker should still hold the repo lock"
    await bus.r.delete(*held)

    deps = _deps(migrated_pg_url, redis_url, str(host_tmp), ScriptedAgents())
    try:
        state, resumed = await load_state(deps, run_id)
        assert resumed and state.phase is Phase.CODE
        assert state.plan is not None and state.tasks, "the plan must survive the crash"

        res = RunResources()
        await reattach(state, deps, res, worker_id())

        final = await run(state, deps, res)
        assert final.phase is Phase.DONE, final.error

        async with session(deps.engine) as s:
            steps = await db.list_steps(s, run_id)
        agents = [st.agent for st in steps]
        assert agents.count("planner") == 1, agents
        assert agents.count("analyzer") == 1, agents
        assert agents.count("decomposer") == 1, agents
        assert agents.count("coder") >= 1, agents

        after = await _phases_checkpointed(engine, run_id)
        assert after[: len(before)] == before, "resume must extend the checkpoint log"
        assert after[-1] == Phase.DONE.value
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE runs CASCADE"))
        await deps.bus.close()
        await deps.engine.dispose()
        await bus.close()
        await engine.dispose()
