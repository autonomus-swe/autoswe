"""One async function per phase. Each takes and returns the run state."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from agents.coder import CoderAgent
from contracts import TaskSpec, TestReport
from core.errors import SandboxError
from gateway.routing import route_for
from observability.logging import bind_run, get_logger
from orchestrator.deps import Deps
from orchestrator.hooks import OrchestratorHooks
from orchestrator.state import Phase, RunState
from repo import worktree as wt
from repo.clone import ensure_bare_clone, repo_key, resolve_sha
from repo.gitcmd import git
from repo.github import open_pr, pr_body, push_branch
from repo.worktree import Worktree
from sandbox.base import Sandbox
from storage import repo as db
from storage.db import session
from tools.base import RunContext
from tools.tests import RunTestsTool

log = get_logger(__name__)

LOCK_TTL_S = 60
LOCK_RENEW_S = 20
INSTALL_TIMEOUT_S = 900


@dataclass
class RunResources:
    """Everything a run allocates and must give back, whatever happens."""

    worktree: Worktree | None = None
    sandbox: Sandbox | None = None
    lock_key: str | None = None
    lock_owner: str | None = None
    renewer: asyncio.Task[None] | None = None
    view_hashes: dict[str, str] = field(default_factory=dict)


Node = Callable[[RunState, Deps, RunResources], Awaitable[RunState]]


async def _emit(deps: Deps, run_id: UUID, type: str, payload: dict[str, Any]) -> None:
    async with session(deps.engine) as s:
        await db.insert_event(s, run_id, type, payload)
    with contextlib.suppress(Exception):  # a dead bus must not fail the run
        await deps.bus.emit(run_id, type, payload)


async def _renew_forever(deps: Deps, key: str, owner: str) -> None:
    while True:
        await asyncio.sleep(LOCK_RENEW_S)
        await deps.bus.renew_lock(key, owner, LOCK_TTL_S)


def install_command(worktree: Path) -> str | None:
    """The dependency install for this repo, or None when there is nothing to install."""
    if (worktree / "pyproject.toml").is_file():
        return "uv sync --all-extras || uv sync"
    if (worktree / "requirements.txt").is_file():
        return "uv venv && uv pip install -r requirements.txt"
    return None


def synthetic_task(goal: str) -> TaskSpec:
    """v1 has no planner: the goal itself is the single task."""
    return TaskSpec(
        id="t1",
        title=goal[:80],
        description=goal,
        depends_on=[],
        files=[],
        acceptance_criteria=["All tests pass"],
        test_selector="",
    )


async def setup_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    res.lock_key = f"lock:repo:{repo_key(state.repo_url)}:{state.base_branch}"
    res.lock_owner = f"worker-{uuid4()}"
    if not await deps.bus.acquire_lock(res.lock_key, res.lock_owner, LOCK_TTL_S):
        holder = await deps.bus.lock_owner(res.lock_key)
        res.lock_key = None
        raise RuntimeError(f"another run holds {state.repo_url}@{state.base_branch} ({holder})")
    res.renewer = asyncio.create_task(_renew_forever(deps, res.lock_key, res.lock_owner))

    token = deps.settings.github_token.get_secret_value() if deps.settings.github_token else None
    bare = await ensure_bare_clone(state.repo_url, deps.repos_dir(), token)
    state.base_sha = await resolve_sha(bare, state.base_branch)
    res.worktree = await wt.create(bare, deps.worktrees_dir(), state.run_id, state.base_branch)

    sandbox = deps.sandbox_factory(state.run_id, res.worktree.path)
    res.sandbox = sandbox
    await sandbox.start()

    install = install_command(res.worktree.path)
    if install:
        await sandbox.connect_install_network()
        result = await sandbox.exec(install, timeout_s=INSTALL_TIMEOUT_S)
        if not result.ok:
            log.warning("install_failed", exit_code=result.exit_code, stderr=result.stderr[-800:])

    await sandbox.disconnect_network()
    if await sandbox.has_network():  # the Coder must never start with network access
        raise SandboxError("sandbox still has network access after disconnect")

    state.task = synthetic_task(state.goal)
    await _emit(deps, state.run_id, "phase_changed", {"phase": Phase.CODE.value})
    return state


def _run_context(state: RunState, res: RunResources, step_id: UUID, role: str) -> RunContext:
    assert res.sandbox is not None and res.worktree is not None
    return RunContext(
        run_id=state.run_id,
        step_id=step_id,
        role=role,
        sandbox=res.sandbox,
        worktree=res.worktree.path,
        work_branch=state.work_branch,
        base_sha=state.base_sha or "HEAD",
        test_command=state.test_command,
        view_hashes=res.view_hashes,
    )


async def code_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    assert state.task is not None
    async with session(deps.engine) as s:
        step_id = await db.start_step(
            s,
            run_id=state.run_id,
            task_id=state.task.id,
            agent="coder",
            phase=Phase.CODE.value,
            input={"goal": state.goal, "task": state.task.model_dump(mode="json")},
        )
    bind_run(state.run_id, task_id=state.task.id, step_id=step_id)
    route = route_for("coder")
    hooks = OrchestratorHooks(
        run_id=state.run_id,
        step_id=step_id,
        engine=deps.engine,
        bus=deps.bus,
        provider_name=deps.provider.provider_name,
        model=deps.provider.model,
        effort=route.effort,
    )
    ctx = _run_context(state, res, step_id, "coder")
    error: str | None = None
    try:
        result, outcome = await CoderAgent().run(deps.provider, ctx, state.goal, state.task, hooks)
        state.task_result = result
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        state.usage = state.usage.add(hooks.usage)
        async with session(deps.engine) as s:
            await db.finish_step(
                s,
                step_id,
                output=state.task_result.model_dump(mode="json") if state.task_result else None,
                error=error,
                usage=hooks.usage,
            )
            await db.set_run_cost(s, state.run_id, state.usage.cost_usd)
    log.info("coder_done", turns=outcome.turns, tool_calls=hooks.tool_calls)
    await _emit(deps, state.run_id, "phase_changed", {"phase": Phase.TEST.value})
    return state


async def test_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Deterministic: no model call, just the tool and its parsed report."""
    async with session(deps.engine) as s:
        step_id = await db.start_step(
            s, run_id=state.run_id, task_id=None, agent="tester", phase=Phase.TEST.value
        )
    ctx = _run_context(state, res, step_id, "tester")
    result = await RunTestsTool()(ctx, selector="")
    report = TestReport.model_validate(result.artifact)
    state.last_test_report = report
    async with session(deps.engine) as s:
        await db.insert_tool_call(
            s,
            step_id=step_id,
            name="run_tests",
            input={"selector": ""},
            output_preview=result.content[:2000],
            exit_code=0 if report.passed else 1,
            duration_ms=int(report.duration_s * 1000),
        )
        await db.save_artifact(s, state.run_id, "test_report", None, report.model_dump(mode="json"))
        await db.finish_step(
            s,
            step_id,
            output=report.model_dump(mode="json"),
            error=None,
            usage=state.usage.model_copy(update={"cost_usd": 0.0}),
        )
    await _emit(
        deps,
        state.run_id,
        "test_report",
        {"passed": report.passed, "total": report.total, "failed": report.failed},
    )
    return state


async def pr_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    assert res.worktree is not None
    report = state.last_test_report
    await push_branch(res.worktree.path, state.work_branch, deps.git_token())
    state.pushed = True
    diff_stat = await git("diff", "--stat", state.base_sha or "HEAD", cwd=res.worktree.path)
    summary = (
        f"{report.total - report.failed - report.errors} passed in {report.duration_s:.2f}s"
        if report
        else "not run"
    )
    if deps.github is None:
        raise RuntimeError("GITHUB_TOKEN is required to open a pull request")
    state.pr_url = await open_pr(
        state.repo_url,
        head=state.work_branch,
        base=state.base_branch,
        title=f"{state.goal[:70]}",
        body=pr_body(
            goal=state.goal,
            diff_stat=diff_stat,
            test_summary=summary,
            run_id=str(state.run_id),
        ),
        client=deps.github,
    )
    async with session(deps.engine) as s:
        await db.finish_run(s, state.run_id, status="done", pr_url=state.pr_url)
    await _emit(deps, state.run_id, "pr_opened", {"pr_url": state.pr_url})
    return state


NODES: dict[Phase, Node] = {
    Phase.SETUP: setup_node,
    Phase.CODE: code_node,
    Phase.TEST: test_node,
    Phase.PR: pr_node,
}


async def teardown(state: RunState, deps: Deps, res: RunResources) -> None:
    """Always runs. Never raises: a teardown failure must not mask the real error."""
    if res.renewer is not None:
        res.renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await res.renewer
    if res.sandbox is not None:
        keep = deps.settings.keep_failed_sandbox and state.phase is Phase.FAILED
        with contextlib.suppress(Exception):
            await res.sandbox.stop(remove=not keep)
    if res.worktree is not None and state.pushed:
        with contextlib.suppress(Exception):
            await wt.remove(res.worktree)
    if res.lock_key and res.lock_owner:
        with contextlib.suppress(Exception):
            await deps.bus.release_lock(res.lock_key, res.lock_owner)
