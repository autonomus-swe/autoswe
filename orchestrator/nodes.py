"""One async function per phase. Each takes and returns the run state."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from agents import pr_writer, reviewer, security, tester
from agents.analyzer import AnalyzerAgent
from agents.base import fence
from agents.coder import CoderAgent
from agents.debugger import DebuggerAgent
from agents.decomposer import DecomposerAgent
from agents.planner import PlannerAgent
from contracts import (
    DebugHypothesis,
    RepoFacts,
    ReviewFinding,
    ReviewReport,
    SecurityReport,
    Task,
    TaskGraph,
    TaskGraphSpec,
    TaskSpec,
    TestReport,
)
from core.errors import AgentError, BudgetExhausted, RepoError, SandboxError
from gateway import caching
from gateway.routing import DOWNGRADE, ROUTES, Route, route_for
from observability import metrics
from observability.logging import bind_run, get_logger
from orchestrator.approvals import ApprovalGate
from orchestrator.budgets import BudgetGate
from orchestrator.deps import Deps
from orchestrator.events import emit
from orchestrator.hooks import OrchestratorHooks
from orchestrator.state import Phase, RunState
from orchestrator.transition import MAX_DEBUG_ATTEMPTS
from repo import diff, embeddings, graph, pr_body, repomap, symbols
from repo import profile as repo_profile
from repo import worktree as wt
from repo.clone import ensure_bare_clone, repo_key, resolve_sha
from repo.gitcmd import git
from repo.github import gitleaks_gate, open_pr, push_branch
from repo.repomap import render_map
from repo.worktree import Worktree
from sandbox import select
from sandbox.base import Sandbox
from storage import repo as db
from storage.db import session
from tools import scanners
from tools.base import RunContext
from tools.tests import RunTestsTool

log = get_logger(__name__)

LOCK_TTL_S = 60
INBOX_POLL_S = 15
AWAITING_INPUT_TIMEOUT_S = 24 * 3600
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
    # The embedding pass, which runs while ANALYZE and PLAN do. Held so teardown can
    # cancel it rather than leaving it writing rows after the run is over.
    embedding_task: asyncio.Task[None] | None = None
    # Repository facts, conventions and map, rendered once and then left alone. It is the
    # cached half of every later request's prefix, so rebuilding it per step — even to the
    # same bytes — is a risk with no upside. See gateway/caching.
    run_block: str | None = None
    view_hashes: dict[str, str] = field(default_factory=dict)


Node = Callable[[RunState, Deps, RunResources], Awaitable[RunState]]


async def _emit(deps: Deps, run_id: UUID, type: str, payload: dict[str, Any]) -> None:
    await emit(deps.bus, deps.engine, run_id, type, payload)


async def _renew_forever(deps: Deps, key: str, owner: str) -> None:
    while True:
        await asyncio.sleep(LOCK_RENEW_S)
        await deps.bus.renew_lock(key, owner, LOCK_TTL_S)


# run_tests needs these in the *project* venv: `uv run --no-sync pytest` ignores anything
# installed globally in the image, so they are installed while the network is still up.
HARNESS_PACKAGES = "pytest pytest-json-report pytest-timeout"


def install_command(worktree: Path, facts: RepoFacts | None = None) -> str:
    """The dependency install for this repo, always ending with the test harness.

    Takes the command from the detected RepoFacts when there are any; the fallback keeps
    a repository with no recognised manifest working.
    """
    detected = facts.install_command if facts else None
    if detected:
        deps = f"({detected})"
    elif (worktree / "pyproject.toml").is_file():
        deps = "(uv sync --all-extras || uv sync)"
    elif (worktree / "requirements.txt").is_file():
        deps = "uv venv && uv pip install -r requirements.txt"
    else:
        deps = "uv venv"
    return f"{deps} && uv pip install {HARNESS_PACKAGES}"


MAX_CODER_FILES = 6
MAX_CODER_FILE_LINES = 300


def synthetic_task(goal: str) -> TaskSpec:
    """The whole goal as one task. Used only when a run has no task graph."""
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

    # Deterministic facts first: SETUP uses the detected install command, ANALYZE gets them
    # as context rather than re-deriving what a file read can settle, and — since this
    # step — the *container itself* is chosen from them. `collect` is a host-side walk of a
    # worktree that already exists; it needs no sandbox, so the only thing that ever kept
    # it below this line was the order it was written in.
    state.facts = repo_profile.collect(res.worktree.path)
    # Pinned on the state, not re-derived later. A resumed run must get the image its first
    # attempt got: retries cross deploys, and re-deciding would let a change to the mapping
    # swap the toolchain under a half-finished run.
    sandbox = deps.sandbox_factory(state.run_id, res.worktree.path, state.facts)
    res.sandbox = sandbox
    # Read back rather than predicted: the factory falls back to the default image when the
    # chosen one was never built on this host, and pinning what we *asked* for would make
    # every resume repeat the same failure.
    state.sandbox_image = sandbox.image
    limits = select.limits_for(state.facts, sandbox.image)
    # Before `start`, not after: starting is the step that fails when an image is missing
    # or the daemon is unhappy, and the first question then is which image it was.
    log.info(
        "sandbox_selected",
        image=state.sandbox_image,
        package_manager=state.facts.package_manager,
        files=state.facts.file_count,
        mem_limit=limits.mem_limit,
        tmpfs=limits.tmpfs_size,
    )
    await sandbox.start()

    # The symbol index, host-side and before the sandbox needs to exist: it reads files and
    # writes rows and wants a container for neither. Keyed by the base SHA, so a second run
    # against the same commit reuses it — which is what makes it affordable on a large
    # repository. A failure here costs the repo map its ranking, not the run: ANALYZE and
    # the Coder both work without it.
    try:
        await symbols.index_repo(deps.engine, res.worktree.path, state.base_sha or "HEAD")
    except Exception as e:
        log.warning("symbol_index_failed", error=f"{type(e).__name__}: {e}")

    # Embeddings, in the background. The phase document says ANALYZE and PLAN must not wait
    # for it, and they do not: the task is left running and whoever calls `search_code`
    # first either finds an index or is told there is none and gets text search instead.
    #
    # Held on `res` so teardown can cancel it. A task nobody holds is a task that outlives
    # the run and writes rows for a commit nothing is working on any more.
    res.embedding_task = asyncio.create_task(
        _index_embeddings(deps, res.worktree.path, state.base_sha or "HEAD")
    )
    # The install is the only CPU-bound part of a run — a Go build, a node-gyp build — and
    # it is also the only part with a network. Everything after is mostly waiting on a
    # model, so the extra cores are handed back rather than held for the whole run.
    await sandbox.connect_install_network()
    if limits.raises_cpus_to_install:
        await sandbox.set_cpus(limits.install_cpus)
    install = install_command(res.worktree.path, state.facts)
    try:
        # Only here. The proxy exists for the one window with a network; setting these at
        # container creation would leave them in the environment of every command the agent
        # runs afterwards, pointing at a proxy that is by then unreachable.
        result = await sandbox.exec(
            install, timeout_s=INSTALL_TIMEOUT_S, env=deps.settings.proxy_env()
        )
    finally:
        # In a `finally` because `exec` raises on a hung command or a container that went
        # away, and an install that died holding four cores would keep them for the rest
        # of the run — on a worker running two jobs, that is the other run's share.
        if limits.raises_cpus_to_install:
            await sandbox.set_cpus(limits.cpus)
    if not result.ok:
        # Not fatal on its own: the repo may vendor its dependencies. TEST will say so
        # clearly if the suite cannot run, and the output is on the step for diagnosis.
        log.warning(
            "install_failed",
            exit_code=result.exit_code,
            output=(result.stdout + result.stderr)[-1500:],
        )

    await sandbox.disconnect_network()
    if await sandbox.has_network():  # the Coder must never start with network access
        raise SandboxError("sandbox still has network access after disconnect")

    await _emit(deps, state.run_id, "phase_changed", {"phase": Phase.ANALYZE.value})
    return state


async def _index_embeddings(deps: Deps, worktree: Path, repo_sha: str) -> None:
    """Embed this commit's definitions, off the critical path.

    Reads the symbol index rather than re-parsing — it was just written, by the line above
    the task that starts this.
    """
    try:
        async with session(deps.engine) as s:
            rows = await db.symbols_for_sha(s, repo_sha)
        if not rows:
            return
        indexed = [
            symbols.Symbol(
                path=r.path,
                kind=r.kind,
                name=r.name,
                signature=r.signature,
                start_line=r.start_line,
                end_line=r.end_line,
                refs=list(r.refs or []),
            )
            for r in rows
        ]
        await embeddings.index_repo(deps.engine, worktree, repo_sha, indexed)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("embedding_task_failed", error=f"{type(e).__name__}: {e}")


def _approval_gate(state: RunState, deps: Deps, res: RunResources) -> ApprovalGate:
    """The gate for this run, wired to keep the lock alive and the wait out of the budget."""

    async def renew() -> None:
        if res.lock_key and res.lock_owner:
            await deps.bus.renew_lock(res.lock_key, res.lock_owner, LOCK_TTL_S)

    def on_wait(seconds: float) -> None:
        state.waiting_s += seconds

    return ApprovalGate(
        run_id=state.run_id,
        engine=deps.engine,
        bus=deps.bus,
        unattended=state.unattended,
        on_wait=on_wait,
        renew=renew,
    )


async def _baseline(state: RunState, deps: Deps, res: RunResources) -> set[str]:
    """Which tests were already failing before the agent touched anything.

    Real repositories have failing and environment-dependent tests, and a run that is
    blamed for inheriting them never finishes. So the suite is run once and every
    signature it produces is excused later.

    Two things about *when* this runs, both deliberate:

    The phase document puts it in SETUP. It is at the end of ANALYZE instead, because
    ANALYZE is where the test command stops being a guess. A baseline taken with the
    default command would describe a different set of tests from the one the agent's own
    runs execute, and a comparison across two test sets is not a comparison.

    It runs after the network has been disconnected, so the baseline is taken under the
    conditions the agent will face. Taken with network access, a test that needs the
    internet looks green here and red later, and the agent is blamed for it.

    Failing to establish a baseline is not fatal. An empty baseline excuses nothing, which
    is the strict reading, and strict is the safe direction to be wrong in.
    """
    if not state.test_command.strip():
        return set()
    repo = repo_key(state.repo_url)
    sha = state.base_sha or ""
    cached = await deps.bus.get_baseline(repo, sha) if sha else None
    if cached is not None:
        log.info("baseline_cached", count=len(cached), sha=sha[:12])
        return set(cached)

    async with session(deps.engine) as s:
        step_id = await db.start_step(
            s, run_id=state.run_id, task_id=None, agent="tester", phase=Phase.ANALYZE.value
        )
    report: TestReport | None = None
    preview = ""
    error: str | None = None
    try:
        ctx = _run_context(state, res, step_id, "tester", deps.engine)
        result = await RunTestsTool()(ctx, selector="")
        report = TestReport.model_validate(result.artifact)
        preview = result.content[:2000]
    except Exception as e:  # see the docstring: strict, not fatal
        error = f"{type(e).__name__}: {e}"
        log.warning("baseline_failed", error=error)
    signatures = sorted({f.signature for f in report.failures if f.signature}) if report else []
    async with session(deps.engine) as s:
        if report is not None:
            # In the ledger like any other action: this ran the repository's whole test
            # suite, and a run's audit trail that omits it cannot explain what was excused.
            await db.insert_tool_call(
                s,
                step_id=step_id,
                name="run_tests",
                input={"selector": "", "baseline": True},
                output_preview=preview,
                exit_code=0 if report.passed else 1,
                duration_ms=int(report.duration_s * 1000),
            )
            await db.save_artifact(
                s, state.run_id, "baseline_report", None, report.model_dump(mode="json")
            )
        await db.finish_step(
            s,
            step_id,
            output={"signatures": signatures, "failures": len(signatures)},
            error=error,
            usage=state.usage.model_copy(update={"cost_usd": 0.0}),
        )
    if report is not None and sha:
        await deps.bus.set_baseline(repo, sha, signatures)
    log.info("baseline_recorded", count=len(signatures), sha=sha[:12])
    await _emit(
        deps,
        state.run_id,
        "test_report",
        {
            "baseline": True,
            "passed": bool(report and report.passed),
            "total": report.total if report else 0,
            "failed": len(signatures),
        },
    )
    return set(signatures)


def _run_context(
    state: RunState, res: RunResources, step_id: UUID, role: str, engine: object = None
) -> RunContext:
    assert res.sandbox is not None and res.worktree is not None
    return RunContext(
        engine=engine,
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


async def _repo_map(state: RunState, deps: Deps, res: RunResources) -> str:
    """The map the reading agents are given: ranked by centrality and by the goal.

    Reads the symbol index rather than re-parsing. The index is built once in SETUP and
    keyed by the base SHA, so this costs a query and a graph build instead of walking three
    thousand files three times a run.

    Falls back to the v1 tree whenever the index is empty — an unsupported language, a
    repository nothing parsed, or an indexing pass that failed. A worse map beats no map,
    and the three callers cannot tell the difference.
    """
    assert res.worktree is not None
    pinned = list(state.plan.affected_files) if state.plan else []
    try:
        async with session(deps.engine) as s:
            rows = await db.symbols_for_sha(s, state.base_sha or "")
        indexed = [
            symbols.Symbol(
                path=r.path,
                kind=r.kind,
                name=r.name,
                signature=r.signature,
                start_line=r.start_line,
                end_line=r.end_line,
                refs=list(r.refs or []),
            )
            for r in rows
        ]
        if indexed:
            files = sorted({sym.path for sym in indexed})
            ranks = graph.build(res.worktree.path, files, indexed).rank
            rendered = repomap.render_symbol_map(
                res.worktree.path, indexed, ranks, goal=state.goal, pinned=pinned
            )
            if rendered:
                return rendered
    except Exception as e:
        # The map is context, not a gate. A run that cannot rank its files is worse at
        # choosing where to look and is not wrong about anything.
        log.warning("repo_map_v2_failed", error=f"{type(e).__name__}: {e}")
    return render_map(res.worktree.path)


async def _ensure_run_block(state: RunState, deps: Deps, res: RunResources) -> str:
    """The repository context every looping agent shares, built once and then frozen.

    Built lazily rather than at the end of ANALYZE because a resumed run starts in a fresh
    process with an empty `RunResources` and never re-enters that node. Lazily and *once*:
    the value is the cached half of the request prefix, so rebuilding it per step — even to
    the same bytes, which the goal-ranked map does not guarantee — would spend the cache
    for nothing.
    """
    if res.run_block is None:
        rendered = caching.render_run_block(
            state.facts, state.repo, await _repo_map(state, deps, res)
        )
        # Fenced, because this block sits in `system` and everything in it is derived from
        # the repository: file names the repository chose, signatures it wrote, and a
        # profile a model summarised from both. Unfenced in `system` it would read as the
        # operator's own instructions, which is a worse place for it than the messages it
        # used to travel in.
        res.run_block = fence("repository context", rendered) if rendered else ""
        log.info("run_block_built", chars=len(res.run_block))
    return res.run_block


async def analyze_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Survey the repository. Fills state.repo and the test command every later phase uses."""
    assert res.worktree is not None
    facts = state.facts or repo_profile.collect(res.worktree.path)
    state.facts = facts
    repo_map = await _repo_map(state, deps, res)

    step_id, hooks, ctx, route = await _begin(state, deps, res, "analyzer", Phase.ANALYZE)
    error: str | None = None
    profile = None
    try:
        profile, _outcome = await AnalyzerAgent(tier=route.tier).run(
            deps.provider, ctx, state.goal, facts, repo_map, hooks
        )
        state.repo = profile
        state.test_command = profile.test_command
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        await _end(state, deps, step_id, hooks, profile, error)
    # Now that the test command is known rather than guessed. See _baseline.
    state.baseline_failures = await _baseline(state, deps, res)
    await _emit(deps, state.run_id, "phase_changed", {"phase": Phase.PLAN.value})
    return state


async def plan_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Produce the implementation plan. Open questions park the run for a human."""
    assert res.worktree is not None and state.repo is not None
    repo_map = await _repo_map(state, deps, res)
    step_id, hooks, ctx, route = await _begin(state, deps, res, "planner", Phase.PLAN)
    error: str | None = None
    plan = None
    try:
        plan, _outcome = await PlannerAgent(tier=route.tier).run(
            deps.provider,
            ctx,
            state.goal,
            state.repo,
            repo_map,
            hooks,
            answers=state.answers,
            unattended=state.unattended,
        )
        state.plan = plan
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        await _end(state, deps, step_id, hooks, plan, error, extra_input={"answers": state.answers})
    return state


async def decompose_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Turn the plan into a validated task graph."""
    assert res.worktree is not None and state.repo is not None and state.plan is not None
    repo_map = await _repo_map(state, deps, res)
    step_id, hooks, _ctx, route = await _begin(state, deps, res, "decomposer", Phase.DECOMPOSE)
    error: str | None = None
    graph = None
    try:
        graph = await DecomposerAgent(tier=route.tier).run(
            deps.provider, state.goal, state.plan, state.repo, repo_map
        )
        state.tasks = graph
        async with session(deps.engine) as s:
            await db.upsert_tasks(s, state.run_id, graph)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        await _end(state, deps, step_id, hooks, graph, error)
    await _emit(
        deps,
        state.run_id,
        "phase_changed",
        {"phase": Phase.CODE.value, "tasks": len(graph.tasks) if graph else 0},
    )
    return state


async def awaiting_input_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Park until a human answers, the run is cancelled, or the wait times out."""
    questions = state.plan.open_questions if state.plan else []
    joined = " ".join(questions)
    async with session(deps.engine) as s:
        await db.set_run_phase(s, state.run_id, Phase.AWAITING_INPUT.value, "awaiting_input")
    await _emit(
        deps,
        state.run_id,
        "awaiting_input",
        {"kind": "open_questions", "questions": questions},
    )

    started = time.monotonic()
    while True:
        message = await deps.bus.pop_inbox(state.run_id, timeout_s=INBOX_POLL_S)
        if message and message.get("type") == "answer":
            state.answers.append((joined, str(message.get("text", ""))))
            state.waiting_s += time.monotonic() - started
            await _emit(deps, state.run_id, "log", {"message": "answer received"})
            return state
        if await deps.bus.is_cancelled(state.run_id):
            state.cancelled = True
            state.waiting_s += time.monotonic() - started
            return state
        if res.lock_key and res.lock_owner:  # keep holding the repo while we wait
            await deps.bus.renew_lock(res.lock_key, res.lock_owner, LOCK_TTL_S)
        if time.monotonic() - started > AWAITING_INPUT_TIMEOUT_S:
            state.waiting_s += time.monotonic() - started
            state.error = "no answer within the waiting period"
            state.phase = Phase.FAILED
            return state


async def _begin(
    state: RunState, deps: Deps, res: RunResources, agent: str, phase: Phase
) -> tuple[UUID, OrchestratorHooks, RunContext, Route]:
    """Open a step, its hooks, a run context and the route it was given.

    The route comes back so the node can build its agent on the same tier the hooks are
    recording. Computing it twice would let the two disagree — the ledger would name one
    model and the request would use another the moment a reconciliation landed between
    the two calls.
    """
    step_input: dict[str, Any] = {"goal": state.goal}
    if state.answers:
        # a re-plan after a pause is driven by what the human said; the audit trail has
        # to show it, or the step looks identical to the one that asked the question
        step_input["answers"] = [{"questions": q, "answer": a} for q, a in state.answers]
    async with session(deps.engine) as s:
        step_id = await db.start_step(
            s,
            run_id=state.run_id,
            task_id=None,
            agent=agent,
            phase=phase.value,
            input=step_input,
        )
    bind_run(state.run_id, step_id=step_id)
    await _emit(deps, state.run_id, "agent_started", {"agent": agent})
    route = _route(state, deps, agent)
    # The context comes first: the hooks hold its `submitted` dict by reference so a gate
    # can see a submission the moment a tool records it, mid-loop.
    ctx = _run_context(state, res, step_id, agent, deps.engine)
    hooks = OrchestratorHooks(
        run_id=state.run_id,
        step_id=step_id,
        engine=deps.engine,
        bus=deps.bus,
        provider_name=deps.provider.provider_name,
        model=deps.provider.model_for(route.tier),
        effort=route.effort,
        role=agent,
        submitted=ctx.submitted,
        approvals=_approval_gate(state, deps, res),
        answers=ctx.answers,
        budget=_budget_gate(state, deps),
    )
    return step_id, hooks, ctx, route


def _tester_hooks(state: RunState, deps: Deps, step_id: UUID) -> OrchestratorHooks:
    """Hooks for the TEST phase, which is deterministic apart from one triage call."""
    route = _route(state, deps, "tester")
    return OrchestratorHooks(
        run_id=state.run_id,
        step_id=step_id,
        engine=deps.engine,
        bus=deps.bus,
        provider_name=deps.provider.provider_name,
        model=deps.provider.model_for(route.tier),
        effort=route.effort,
        role="tester",
        budget=_budget_gate(state, deps),
    )


def _downgraded_roles(deps: Deps) -> list[str]:
    """Which roles a downgrade would actually move to a different model.

    Not simply `sorted(DOWNGRADE)`: on a single-model deployment every tier resolves to
    the same model, so the downgrade is real policy with no effect. Reporting those roles
    as downgraded would be claiming a saving that was never made, and the first person to
    check the ledger against the event would stop trusting both.
    """
    return sorted(
        role
        for role, tier in DOWNGRADE.items()
        if deps.provider.model_for(tier) != deps.provider.model_for(ROUTES[role].tier)
    )


def _route(state: RunState, deps: Deps, role: str) -> Route:
    """The route for this step, downgraded if the run is short of money.

    Read fresh at every step rather than latched: `state.usage` is reconciled from
    `llm_calls`, so this follows the real number in both directions. A run that crosses the
    line and is then refunded — a provider correcting a charge, a reconciliation finding a
    step double-counted — goes back to the better model rather than staying punished.
    """
    downgrade = state.budget.past_cost_warning(
        state.usage, state.elapsed_s(), cost_measurable=state.cost_measurable
    )
    route = route_for(role, downgrade=downgrade)
    if route != ROUTES[role]:
        # A new cache namespace for this role: the prefix is keyed per model, so the first
        # call after a downgrade writes rather than reads. Logged because a cache hit rate
        # that falls off a cliff mid-run otherwise looks like the caching broke.
        log.info(
            "role_downgraded",
            role=role,
            was=deps.provider.model_for(ROUTES[role].tier),
            now=deps.provider.model_for(route.tier),
        )
    return route


def _budget_gate(state: RunState, deps: Deps) -> BudgetGate:
    """The step's view of spending. Holds the run's ``warned`` set by reference, so a
    warning is emitted once per run rather than once per phase."""

    async def warn(kind: str, fraction: float) -> None:
        payload: dict[str, Any] = {"kind": kind, "fraction": round(fraction, 3)}
        if kind == "budget_usd":
            # Named on the warning rather than left to be inferred from the ledger: the
            # next few steps cost less and produce different-looking output, and a reader
            # who cannot see why would read it as the run degrading on its own.
            payload["downgraded"] = _downgraded_roles(deps)
        await _emit(deps, state.run_id, "budget_warning", payload)

    return BudgetGate(
        run_id=state.run_id,
        engine=deps.engine,
        budget=state.budget,
        elapsed=state.elapsed_s,
        warned=state.warned,
        cost_measurable=state.cost_measurable,
        on_warning=warn,
        usage=state.usage,
    )


async def _end(
    state: RunState,
    deps: Deps,
    step_id: UUID,
    hooks: OrchestratorHooks,
    output: Any,
    error: str | None,
    extra_input: dict[str, Any] | None = None,
) -> None:
    payload = output.model_dump(mode="json") if hasattr(output, "model_dump") else None
    async with session(deps.engine) as s:
        await db.finish_step(s, step_id, output=payload, error=error, usage=hooks.usage)
        # Reconciled from llm_calls rather than accumulated in memory. The in-memory sum
        # drifts: a step that crashed after its rows were written still spent the money,
        # and a resumed run starts from a checkpoint that never saw it.
        state.usage = await db.run_cost(s, state.run_id)
        await db.set_run_cost(s, state.run_id, state.usage.cost_usd)
    if extra_input:
        async with session(deps.engine) as s:
            await db.save_artifact(s, state.run_id, "step_input", None, extra_input)
    await _emit(deps, state.run_id, "agent_finished", {"error": error})


async def code_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """One task from the graph, with a fresh context and no sight of earlier transcripts."""
    if state.tasks is None:  # a run with no plan still has work to do
        state.tasks = TaskGraph.from_spec(TaskGraphSpec(tasks=[synthetic_task(state.goal)]))
    task_obj = state.tasks.next_ready()
    if task_obj is None:
        raise AgentError("no task is ready to run")
    task = task_obj.spec
    task_obj.status = "in_progress"
    state.current_task_id = task.id
    state.attempts[task.id] = state.attempts.get(task.id, 0)
    # Recorded before the Coder touches anything: a replan after three failed attempts
    # rewinds to here, so it plans against a clean base rather than three half-fixes.
    if task_obj.task_start_sha is None and res.worktree is not None:
        with contextlib.suppress(Exception):
            head = await git("rev-parse", "HEAD", cwd=res.worktree.path)
            task_obj.task_start_sha = head.strip()

    async with session(deps.engine) as s:
        step_id = await db.start_step(
            s,
            run_id=state.run_id,
            task_id=task.id,
            agent="coder",
            phase=Phase.CODE.value,
            input={"goal": state.goal, "task": task.model_dump(mode="json")},
            attempt=state.attempts[task.id],
        )
        await db.upsert_tasks(s, state.run_id, state.tasks)
    bind_run(state.run_id, task_id=task.id, step_id=step_id)
    await _emit(deps, state.run_id, "agent_started", {"agent": "coder", "task_id": task.id})

    route = _route(state, deps, "coder")
    run_block = await _ensure_run_block(state, deps, res)
    ctx = _run_context(state, res, step_id, "coder", deps.engine)
    hooks = OrchestratorHooks(
        run_id=state.run_id,
        step_id=step_id,
        engine=deps.engine,
        bus=deps.bus,
        provider_name=deps.provider.provider_name,
        model=deps.provider.model_for(route.tier),
        effort=route.effort,
        role="coder",
        submitted=ctx.submitted,
        approvals=_approval_gate(state, deps, res),
        answers=ctx.answers,
        budget=_budget_gate(state, deps),
    )
    error: str | None = None
    result = None
    outcome = None
    try:
        result, outcome = await CoderAgent(
            run_block, route.tier, state.budget.task_budget("coder")
        ).run(deps.provider, ctx, state.goal, task, hooks, files=_task_files(res, task))
        state.task_results[task.id] = result
    except AgentError as e:
        # A Coder that did the work and then stopped without submitting has not killed
        # the run. Leaving task_results unset sends `transition` to ESCALATE with
        # `coder_no_result`, which spends one attempt and tries again — the behaviour the
        # escalation table asks for, and a failure mode real weak models produce often.
        error = f"{type(e).__name__}: {e}"
        log.warning("coder_no_result", task_id=task.id, error=error)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        async with session(deps.engine) as s:
            await db.finish_step(
                s,
                step_id,
                output=result.model_dump(mode="json") if result else None,
                error=error,
                usage=hooks.usage,
            )
            state.usage = await db.run_cost(s, state.run_id)
            await db.set_run_cost(s, state.run_id, state.usage.cost_usd)
    log.info(
        "coder_done",
        task_id=task.id,
        turns=outcome.turns if outcome else 0,
        tool_calls=hooks.tool_calls,
        submitted=result is not None,
    )
    await _emit(deps, state.run_id, "agent_finished", {"agent": "coder", "task_id": task.id})
    return state


def _task_files(res: RunResources, task: TaskSpec) -> dict[str, str]:
    """The files the decomposer named, capped. The Coder has read_file for the rest."""
    if res.worktree is None:
        return {}
    out: dict[str, str] = {}
    for name in task.files[:MAX_CODER_FILES]:
        path = res.worktree.path / name
        if not path.is_file():
            continue
        lines = path.read_text(errors="replace").splitlines()
        if len(lines) > MAX_CODER_FILE_LINES:
            out[name] = (
                "\n".join(lines[:MAX_CODER_FILE_LINES])
                + f"\n… {len(lines) - MAX_CODER_FILE_LINES} more lines; use read_file"
            )
        else:
            out[name] = "\n".join(lines)
    return out


async def debug_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """One debug attempt: a hypothesis, then a fix, on the task that just failed.

    The attempt counter goes up here rather than in `transition`, because an attempt is
    something that happened, not something that was decided. A Debugger that crashes
    still consumed an attempt.
    """
    assert state.tasks is not None and state.last_test_report is not None
    task_obj = state.tasks.by_id(state.current_task_id or "")
    task = task_obj.spec
    state.attempts[task.id] = state.attempts.get(task.id, 0) + 1
    task_obj.attempts = state.attempts[task.id]

    async with session(deps.engine) as s:
        step_id = await db.start_step(
            s,
            run_id=state.run_id,
            task_id=task.id,
            agent="debugger",
            phase=Phase.DEBUG.value,
            input={
                "goal": state.goal,
                "task": task.model_dump(mode="json"),
                "report_signature": state.last_test_report.signature,
                "strategy": state.strategy,
            },
            attempt=state.attempts[task.id],
        )
        await db.upsert_tasks(s, state.run_id, state.tasks)
    bind_run(state.run_id, task_id=task.id, step_id=step_id)
    await _emit(deps, state.run_id, "agent_started", {"agent": "debugger", "task_id": task.id})

    route = _route(state, deps, "debugger")
    run_block = await _ensure_run_block(state, deps, res)
    ctx = _run_context(state, res, step_id, "debugger", deps.engine)
    hooks = OrchestratorHooks(
        run_id=state.run_id,
        step_id=step_id,
        engine=deps.engine,
        bus=deps.bus,
        provider_name=deps.provider.provider_name,
        model=deps.provider.model_for(route.tier),
        effort=route.effort,
        role="debugger",
        submitted=ctx.submitted,
        approvals=_approval_gate(state, deps, res),
        answers=ctx.answers,
        budget=_budget_gate(state, deps),
    )
    error: str | None = None
    hypothesis = None
    result = None
    try:
        hypothesis, result, _outcome = await DebuggerAgent(
            run_block, route.tier, state.budget.task_budget("debugger")
        ).run(
            deps.provider,
            ctx,
            state.goal,
            task,
            state.last_test_report,
            hooks,
            previous=await _previous_hypotheses(deps, state.run_id, task.id),
            alternative=state.strategy == "alternative",
            human_hint=task_obj.human_hint,
            context=state.test_context,
        )
        if result is not None:
            state.task_results[task.id] = result
    except BudgetExhausted as e:
        # Out of budget mid-attempt. Not a crash: the attempt is over, the hypothesis is
        # still worth storing, and `transition` reads the budget itself and escalates.
        error = f"budget exhausted: {e}"
        log.info("debug_attempt_out_of_budget", task_id=task.id, reason=str(e))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        async with session(deps.engine) as s:
            # The hypothesis is the record of this attempt, so it is stored whether or
            # not the fix worked; the next TEST decides that.
            await db.finish_step(
                s,
                step_id,
                output=_debug_output(hypothesis, result),
                error=error,
                usage=hooks.usage,
            )
            state.usage = await db.run_cost(s, state.run_id)
            await db.set_run_cost(s, state.run_id, state.usage.cost_usd)
    if hypothesis is not None:
        await _emit(
            deps,
            state.run_id,
            "debug_hypothesis",
            {
                "task_id": task.id,
                "attempt": state.attempts[task.id],
                "failure_class": hypothesis.failure_class,
                "root_cause": hypothesis.root_cause[:400],
                "confidence": hypothesis.confidence,
            },
        )
    log.info(
        "debug_attempt_done",
        task_id=task.id,
        attempt=state.attempts[task.id],
        submitted_result=result is not None,
    )
    return state


def _debug_output(hypothesis: Any, result: Any) -> dict[str, Any]:
    return {
        "hypothesis": hypothesis.model_dump(mode="json") if hypothesis else None,
        "task_result": result.model_dump(mode="json") if result else None,
    }


async def _previous_hypotheses(deps: Deps, run_id: UUID, task_id: str) -> list[DebugHypothesis]:
    """Every hypothesis already tried on this task, oldest first.

    Read from the steps table rather than carried on the state: a resumed run must know
    what its previous attempts believed, and a checkpoint is not the place for a growing
    transcript.
    """
    async with session(deps.engine) as s:
        steps = await db.list_steps(s, run_id)
    out: list[DebugHypothesis] = []
    for step in steps:
        if step.agent != "debugger" or step.task_id != task_id:
            continue
        payload = (step.output or {}).get("hypothesis")
        if payload:
            with contextlib.suppress(ValidationError):
                out.append(DebugHypothesis.model_validate(payload))
    return out


async def escalate_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Decide what a stuck run does next, and set `resume_phase` for the transition.

    This node is the reason a failing loop stops instead of spinning: every path out of
    it either makes a different attempt possible or ends the run. It never retries
    blindly — the same attempt again is what the attempt cap already ruled out.
    """
    reason = state.escalation_reason or "unknown"
    task_obj = (
        state.tasks.by_id(state.current_task_id) if state.tasks and state.current_task_id else None
    )
    await _emit(
        deps,
        state.run_id,
        "escalated",
        {"reason": reason, "task_id": state.current_task_id, "attempts": dict(state.attempts)},
    )
    log.info("escalate", reason=reason, task_id=state.current_task_id)

    # A budget is not a problem the run can solve by trying differently.
    if reason.startswith("budget_"):
        return await _fail(state, deps, res, f"{reason}: the run ran out of its allowance")

    # The Coder produced nothing. That is one failed attempt, not a verdict on the task.
    if reason == "coder_no_result" and task_obj is not None:
        state.attempts[task_obj.id] = state.attempts.get(task_obj.id, 0) + 1
        if state.attempts[task_obj.id] < MAX_DEBUG_ATTEMPTS:
            task_obj.status = "pending"
            state.resume_phase = Phase.CODE
            return state
        reason = "debug_attempts_exhausted"

    if reason != "debug_attempts_exhausted" or task_obj is None:
        return await _fail(state, deps, res, f"escalated with no way forward: {reason}")

    # First exhaustion: rewind and split. Three attempts have left the worktree carrying
    # three half-fixes, so a replan starting from that is planning against noise.
    if not task_obj.replanned:
        await _rewind_task(state, res, task_obj)
        replaced = await _replan_task(state, deps, res, task_obj)
        if replaced:
            state.resume_phase = Phase.CODE
            return state
        log.warning("replan_produced_nothing", task_id=task_obj.id)

    # Already replanned. A human is the only remaining source of new information.
    if state.unattended:
        return await _fail(
            state,
            deps,
            res,
            f"task {task_obj.id} failed {MAX_DEBUG_ATTEMPTS} times after a replan",
        )

    state.resume_phase = Phase.AWAITING_INPUT
    return state


async def _fail(state: RunState, deps: Deps, res: RunResources, error: str) -> RunState:
    """End the run, and open a draft pull request if it got far enough to have commits."""
    state.resume_phase = Phase.FAILED
    state.error = error
    await _draft_pr_for_a_failed_run(state, deps, res)
    return state


async def _draft_pr_for_a_failed_run(state: RunState, deps: Deps, res: RunResources) -> None:
    """A failed run with commits opens a draft `[WIP]` pull request rather than losing them.

    The argument for doing this at all: a run that got three tasks in and died on the
    fourth has produced work somebody can finish, and the alternative is a branch in a
    worktree that teardown deletes. The body says plainly that the run did not complete, so
    the offer is not mistaken for a claim.

    **Never masks the real failure.** `state.error` already holds the diagnosis, and a
    GitHub outage or a missing token must not replace it with its own. Every failure here
    is logged and swallowed — including the secret gate, which means a failed run that also
    committed a credential ends with no pull request and the `gitleaks` artifact on record.
    """
    if res.worktree is None or deps.github is None:
        log.info("no_draft_pr", reason="no worktree or no GitHub client")
        return
    try:
        commits = await git(
            "log",
            "--oneline",
            f"{state.base_sha}..HEAD" if state.base_sha else "HEAD",
            cwd=res.worktree.path,
        )
    except Exception as e:
        log.warning("no_draft_pr", reason=f"could not read the log: {type(e).__name__}: {e}")
        return
    if not commits.strip():
        # Nothing to review. The artifacts still carry what the run learned, and a pull
        # request with no commits in it is a notification rather than a contribution.
        log.info("no_draft_pr", reason="the run made no commits")
        return
    try:
        state.pr_url = await _push_and_open(state, deps, res)
        log.info("draft_pr_opened", pr_url=state.pr_url, commits=len(commits.strip().split("\n")))
    except Exception as e:
        log.warning(
            "draft_pr_failed",
            error=f"{type(e).__name__}: {e}",
            note="the run's own failure is what gets reported",
        )


async def _rewind_task(state: RunState, res: RunResources, task_obj: Any) -> None:
    """Discard the failed attempts' edits, back to where the task started.

    Excludes .venv: it is installed once in SETUP and reinstalling it would cost minutes
    for no benefit, since nothing the agent did put it there.
    """
    if res.worktree is None or not task_obj.task_start_sha:
        return
    with contextlib.suppress(Exception):
        await git("reset", "--hard", task_obj.task_start_sha, cwd=res.worktree.path)
        await git("clean", "-fd", "-e", ".venv", "-e", ".autoswe", cwd=res.worktree.path)
        log.info("task_rewound", task_id=task_obj.id, sha=task_obj.task_start_sha[:8])


async def _replan_task(state: RunState, deps: Deps, res: RunResources, task_obj: Any) -> bool:
    """Split a task that failed its attempts into smaller ones. True when it changed."""
    assert state.tasks is not None
    hypotheses = await _previous_hypotheses(deps, state.run_id, task_obj.id)
    step_id, hooks, _ctx, route = await _begin(state, deps, res, "decomposer", Phase.ESCALATE)
    error: str | None = None
    graph = None
    try:
        graph = await DecomposerAgent(tier=route.tier).replan(
            deps.provider, state.goal, task_obj.spec, state.last_test_report, hypotheses, hooks
        )
    except Exception as e:  # a failed replan is not a crash: the human path is still open
        error = f"{type(e).__name__}: {e}"
        log.warning("replan_failed", task_id=task_obj.id, error=error)
    finally:
        await _end(state, deps, step_id, hooks, graph, error)
    if graph is None or not graph.tasks:
        return False

    # The new tasks stand in for the old one, keeping its place in the ordering so
    # anything that depended on it still depends on all of its parts.
    replacements = [Task(spec=spec) for spec in graph.tasks]
    for r in replacements:
        r.replanned = True
    index = next(i for i, t in enumerate(state.tasks.tasks) if t.id == task_obj.id)
    new_ids = [r.id for r in replacements]
    state.tasks.tasks[index : index + 1] = replacements
    for t in state.tasks.tasks:
        if task_obj.id in t.spec.depends_on:
            t.spec.depends_on = [d for d in t.spec.depends_on if d != task_obj.id] + new_ids
    for r in replacements:
        state.attempts[r.id] = 0
    state.attempts.pop(task_obj.id, None)
    state.current_task_id = None
    state.previous_failure_signature = None
    state.strategy = None
    async with session(deps.engine) as s:
        await db.upsert_tasks(s, state.run_id, state.tasks)
    log.info("task_replanned", task_id=task_obj.id, into=new_ids)
    return True


@dataclass
class _TestRun:
    """One ``run_tests`` call, kept so every run in a TEST phase reaches the database."""

    selector: str
    report: TestReport
    preview: str


async def test_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Run the tests and decide which failures are the agent's.

    Deterministic, with one exception: a failure the parser could not classify is sent to
    a cheap model for a *label*, which is rendered as a note for the Debugger and never
    written back onto the report. Whether the tests passed is decided in Python.
    """
    async with session(deps.engine) as s:
        step_id = await db.start_step(
            s, run_id=state.run_id, task_id=None, agent="tester", phase=Phase.TEST.value
        )
    ctx = _run_context(state, res, step_id, "tester", deps.engine)
    task = state.task
    selector = task.test_selector if task else ""
    runs: list[_TestRun] = []

    async def run(sel: str) -> TestReport:
        result = await RunTestsTool()(ctx, selector=sel)
        report = TestReport.model_validate(result.artifact)
        runs.append(_TestRun(sel, report, result.content[:2000]))
        return report

    targeted = await run(selector) if selector.strip() else None
    flaky: list[str] = []
    inherited: list[str] = []
    if targeted is not None and not targeted.passed:
        # Short-circuit. The selector names the tests this task was written against, so
        # they are never excused as pre-existing: a failure there is the job, not an
        # inheritance. Nor is the full suite informative while they fail.
        report = raw = targeted
    else:
        report = raw = await run("")
        # The baseline is only trusted where the task said what it owns. Without a
        # selector there is no line between "already broken" and "what I was asked to
        # fix", and excusing the second is how a run reports success having done nothing:
        # the tests a goal names are, by definition, failing before it starts.
        if selector.strip():
            report, inherited = tester.filter_baseline(raw, state.baseline_failures)
            loose = tester.outside(report, selector) if not report.passed else []
            if loose and len(loose) == len(report.failures):
                # Everything still failing is in a test this task never claimed. Re-run
                # exactly those, once. If they pass alone they are flaky or
                # order-dependent; one re-run cannot say which, so the ids are recorded
                # and the pull request will list them rather than quietly moving on.
                if (await run(tester.selector_for(loose))).passed:
                    report, flaky = tester.without_flaky(report, loose)
                    log.info("test_flaky", tests=flaky)

    context = tester.source_context(report, ctx.worktree)
    unknown = tester.needs_triage(report.failures)
    if unknown:
        # Hooks even for one call: it is how the triage reaches llm_calls, which is what
        # budgets are enforced from. A model call the run cannot see is worse than one it
        # cannot afford.
        kinds = await tester.TesterAgent(tier=_route(state, deps, "tester").tier).classify_unknown(
            deps.provider, unknown, _tester_hooks(state, deps, step_id)
        )
        for test_id, note in tester.render_triage(kinds).items():
            context[test_id] = f"{note}\n\n{context.get(test_id, '')}".strip()
        async with session(deps.engine) as s:
            state.usage = await db.run_cost(s, state.run_id)

    state.last_test_report = report
    state.test_context = context
    state.flaky_tests |= set(flaky)
    state.preexisting_failures |= set(inherited)
    if report.passed and state.tasks is not None and state.current_task_id is not None:
        state.tasks.by_id(state.current_task_id).status = "done"
        metrics.record_task_done(state.attempts.get(state.current_task_id, 0))
        async with session(deps.engine) as s:
            await db.upsert_tasks(s, state.run_id, state.tasks)
        if state.tasks.next_ready() is None:
            # The last task is done, so the change is final and worth collecting once.
            # Stored here rather than in REVIEW because a resumed run should read the diff
            # it was reviewed against, not recompute one from a worktree that has since
            # moved — and because the artifact is the answer to "what did this run do".
            await _store_diff(state, deps, res)
    async with session(deps.engine) as s:
        for item in runs:
            await db.insert_tool_call(
                s,
                step_id=step_id,
                name="run_tests",
                input={"selector": item.selector},
                output_preview=item.preview,
                exit_code=0 if item.report.passed else 1,
                duration_ms=int(item.report.duration_s * 1000),
            )
        await db.save_artifact(s, state.run_id, "test_report", None, report.model_dump(mode="json"))
        if report.signature != raw.signature:
            # What the runner actually said, before anything was excused. A reviewer
            # asking "why did this pass" needs the unfiltered report to answer it.
            await db.save_artifact(
                s, state.run_id, "test_report_raw", None, raw.model_dump(mode="json")
            )
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
        {
            "passed": report.passed,
            "total": report.total,
            "failed": report.failed,
            "flaky": flaky,
            "pre_existing": inherited,
        },
    )
    return state


async def _store_diff(state: RunState, deps: Deps, res: RunResources) -> None:
    """Collect the run's whole diff and keep it. Never fatal: a run that produced code is
    not worth failing because the record of it could not be written."""
    if res.worktree is None:
        return
    try:
        text = await diff.full_diff(res.worktree.path, state.base_sha or "HEAD")
        files = diff.split_by_file(text)
    except Exception as e:
        log.warning("diff_not_collected", error=f"{type(e).__name__}: {e}")
        return
    async with session(deps.engine) as s:
        await db.save_artifact(
            s,
            state.run_id,
            "diff",
            None,
            {
                "text": text,
                "stat": diff.diff_stat(files),
                "files": [
                    {
                        "path": f.path,
                        "added": f.added,
                        "removed": f.removed,
                        "generated": f.generated,
                    }
                    for f in files
                ],
            },
        )
    log.info("diff_stored", files=len(files), bytes=len(text))


async def _review_diff(state: RunState, deps: Deps, res: RunResources) -> list[diff.FileDiff]:
    """The diff to review: the artifact TEST stored, or a fresh one if it is missing.

    Read back rather than recomputed so a resumed review judges the change it was reviewed
    against. The fallback exists because a review is still worth having if the artifact
    failed to write — and because a run resumed from an older checkpoint may predate it.
    """
    async with session(deps.engine) as s:
        row = await db.latest_artifact(s, state.run_id, "diff")
    if row is not None and isinstance(row.content, dict) and row.content.get("text"):
        return diff.split_by_file(str(row.content["text"]))
    log.info("diff_artifact_missing", note="recomputing from the worktree")
    if res.worktree is None:
        return []
    return diff.split_by_file(await diff.full_diff(res.worktree.path, state.base_sha or "HEAD"))


async def review_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Two passes over the diff: enumerate, then verify against the files.

    The cheap pass is allowed to fail — it leaves the expensive one with nothing to check,
    which is a worse review rather than none, since that pass reads the code itself.
    """
    files = await _review_diff(state, deps, res)
    if not files:
        # Nothing changed, so there is nothing to review. The empty report is written as an
        # artifact and not only onto the state, because "the artifact set is the same shape
        # for every run" is the property a reader relies on: without the row,
        # `GET /runs/{id}/artifacts/review` 404s for a clean run and the caller cannot tell
        # "reviewed, nothing found" from "never reviewed".
        state.review = ReviewReport(findings=[], blocking=False)
        log.info("review_skipped", reason="the run changed no files")
        await _emit(deps, state.run_id, "review_report", {"findings": 0, "blocking": False})
        return state

    run_block = await _ensure_run_block(state, deps, res)
    pre_id, pre_hooks, _, pre_route = await _begin(state, deps, res, "review_pre", Phase.REVIEW)
    candidates: list[ReviewFinding] = []
    error: str | None = None
    try:
        candidates = await reviewer.ReviewPreAgent(run_block, pre_route.tier).run(
            deps.provider, state.goal, state.plan, state.tasks, files, pre_hooks
        )
    except Exception as e:  # the pre-pass is the expendable half
        error = f"{type(e).__name__}: {e}"
        log.warning("review_pre_unusable", error=error)
    finally:
        await _end(state, deps, pre_id, pre_hooks, None, error, None)

    step_id, hooks, ctx, route = await _begin(state, deps, res, "review", Phase.REVIEW)
    report = None
    dropped: list[ReviewFinding] = []
    error = None
    try:
        report, dropped, _outcome = await reviewer.ReviewAgent(run_block, route.tier).run(
            deps.provider, ctx, state.goal, candidates, files, hooks
        )
        state.review = report
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        await _end(state, deps, step_id, hooks, report, error, None)
        if report is not None:
            async with session(deps.engine) as s:
                await db.save_artifact(
                    s,
                    state.run_id,
                    "review",
                    None,
                    # The reasons come off the report itself. Passing nothing here is what
                    # left every stored rejection blank.
                    reviewer.artifact(report, dropped, reviewer.rejection_reasons(report)),
                )

    assert report is not None
    worth_fixing = [f for f in report.findings if f.severity in ("blocking", "major")]
    granted = await _grant_fix_round(
        state,
        deps,
        kind="review",
        phase=Phase.REVIEW,
        blocked=report.blocking,
        spec=lambda n: reviewer.fix_task(worth_fixing, n),
        unresolved=lambda: reviewer.unresolved(worth_fixing),
    )
    await _emit(
        deps,
        state.run_id,
        "review_report",
        {
            "findings": len(report.findings),
            "blocking": report.blocking,
            "candidates": len(candidates),
            "dropped": len(dropped),
            "by_severity": {
                sev: sum(1 for f in report.findings if f.severity == sev)
                for sev in ("blocking", "major", "minor", "nit")
                if any(f.severity == sev for f in report.findings)
            },
            "fix_round": granted,
        },
    )
    return state


async def _grant_fix_round(
    state: RunState,
    deps: Deps,
    *,
    kind: str,
    phase: Phase,
    blocked: bool,
    spec: Callable[[int], TaskSpec],
    unresolved: Callable[[], list[str]],
) -> int | None:
    """Append a fix task when a gate blocks and that gate's budget allows. Returns the round.

    This function owns the round budget, and it is the only thing that does. The phase
    document checked it in `review_node` *and* again in `transition`, with `<` in one place
    and `<=` in the other — which happened to work because a third condition covered the
    difference. Instead, the existence of a ready fix task is the grant: `transition` asks
    whether one is waiting and never counts rounds itself, so the two cannot disagree.

    Taken generic for the security gate rather than copied. A second copy of a budget is how
    the original defect got in, and two gates counting rounds in two functions would be the
    same mistake with a different pair of comparisons. `kind` keys the counter, so the gates
    have separate budgets while sharing the one piece of code that spends them.
    """
    if not blocked or state.tasks is None:
        return None
    used = state.fix_rounds.get(kind, 0)
    if used >= state.budget.max_fix_rounds:
        # Out of rounds. The findings go on the record and the run proceeds — a pull
        # request that names what is wrong with it beats one that never arrives.
        state.known_issues.extend(unresolved())
        log.info("fix_rounds_exhausted", kind=kind, used=used, known_issues=len(state.known_issues))
        return None

    round_n = used + 1
    state.tasks.tasks.append(Task(spec=spec(round_n), kind="fix"))
    state.fix_rounds[kind] = round_n
    state.return_to = phase
    async with session(deps.engine) as s:
        await db.upsert_tasks(s, state.run_id, state.tasks)
    log.info("fix_round_granted", kind=kind, round=round_n)
    return round_n


async def security_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """Four scanners, then one model reading what they found against the files.

    The scanners are the enumerating pass — the same division of labour as the Reviewer's
    two passes, except the cheap half here is mechanical and therefore free to be noisy.

    A scanner pass that cannot run at all is not silently a clean result: `run_all` turns
    each failure into an `info` finding naming the tool, so a broken scanner appears in the
    report as a thing not looked for rather than as nothing found.
    """
    files = await _review_diff(state, deps, res)
    if not files or res.sandbox is None or res.worktree is None:
        # Nothing changed, or no sandbox to scan in. Written as an artifact for the same
        # reason as the review above: an absent row and a clean scan must not look alike.
        state.security = SecurityReport(findings=[], critical=False, checklist={})
        async with session(deps.engine) as s:
            await db.save_artifact(
                s, state.run_id, "security", None, security.artifact(state.security, {})
            )
        log.info("security_skipped", files=len(files), sandbox=res.sandbox is not None)
        await _emit(deps, state.run_id, "security_report", {"findings": 0, "critical": False})
        return state

    found = await scanners.run_all(res.sandbox, res.worktree.path, state.base_sha or "HEAD", files)

    run_block = await _ensure_run_block(state, deps, res)
    step_id, hooks, ctx, route = await _begin(state, deps, res, "security", Phase.SECURITY)
    report = None
    error = None
    try:
        report, _outcome = await security.SecurityAgent(run_block, route.tier).run(
            deps.provider, ctx, state.goal, found, files, hooks
        )
        state.security = report
    except Exception as e:
        # Unlike the Reviewer's pre-pass, this half is not expendable: losing it would
        # leave raw scanner output with nobody's judgement on it, and every unverified
        # finding gates. So the run fails here rather than blocking on unread noise.
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        await _end(state, deps, step_id, hooks, report, error, None)
        if report is not None:
            async with session(deps.engine) as s:
                await db.save_artifact(
                    s,
                    state.run_id,
                    "security",
                    None,
                    security.artifact(report, security.applicable(files)),
                )

    assert report is not None
    gating = security.gating(report.findings)
    granted = await _grant_fix_round(
        state,
        deps,
        kind="security",
        phase=Phase.SECURITY,
        blocked=report.critical,
        spec=lambda n: security.fix_task(gating, n),
        unresolved=lambda: security.unresolved(report.findings),
    )
    await _emit(
        deps,
        state.run_id,
        "security_report",
        {
            "findings": len(report.findings),
            "critical": report.critical,
            "scanner_findings": len(found),
            "gating": len(gating),
            "by_tool": {
                tool: sum(1 for f in report.findings if f.tool == tool)
                for tool in sorted({f.tool for f in report.findings})
            },
            "fix_round": granted,
        },
    )
    return state


async def _body_facts(state: RunState, deps: Deps, res: RunResources) -> pr_body.BodyFacts:
    """Every number the pull request will carry, collected from the run's own records."""
    assert res.worktree is not None
    diff_stat = await git("diff", "--stat", state.base_sha or "HEAD", cwd=res.worktree.path)
    # Only for a run that escalated. On one that succeeded these are noise; on one that
    # did not they are what somebody picking the work up needs most. Read from the steps
    # table rather than the state, which is where `_previous_hypotheses` keeps them.
    hypotheses: dict[str, list[DebugHypothesis]] = {}
    if state.escalation_reason and state.tasks:
        for task in state.tasks.tasks:
            if task.status != "failed" and task.id != state.current_task_id:
                continue
            tried = await _previous_hypotheses(deps, state.run_id, task.id)
            if tried:
                hypotheses[task.id] = tried

    return pr_body.BodyFacts(
        run_id=str(state.run_id),
        goal=state.goal,
        diff_stat=diff_stat,
        plan=state.plan,
        tasks=state.tasks,
        review=state.review,
        security=state.security,
        test_report=state.last_test_report,
        flaky_tests=set(state.flaky_tests),
        preexisting_failures=set(state.preexisting_failures),
        known_issues=list(state.known_issues),
        fix_rounds=dict(state.fix_rounds),
        debug_attempts=sum(state.attempts.values()),
        cost_usd=state.usage.cost_usd,
        elapsed_s=state.elapsed_s(),
        escalation_reason=state.escalation_reason,
        hypotheses=hypotheses,
    )


async def _refuse_nothing_to_propose(state: RunState, res: RunResources) -> None:
    """Refuse to open a pull request that contains no commits.

    Found by the first end-to-end run against a real model. It wrote working code, ran the
    tests in the sandbox — green, because the sandbox tests the *worktree* — submitted its
    result, and never called `git_commit`. The run pushed a branch identical to its base
    and opened an empty pull request, and reported DONE.

    Every scripted provider in the test suite calls `git_commit`, so eight hundred tests
    had never exercised an agent simply forgetting. A pull request with nothing in it is
    worse than none: it reads as success and costs a reviewer the click to find out it is
    not.

    Checked before the push for the same reason the secret scan is: a branch pushed and
    then found empty has already left a dangling ref on the remote.
    """
    assert res.worktree is not None
    commits = await git(
        "log",
        "--oneline",
        f"{state.base_sha}..HEAD" if state.base_sha else "HEAD",
        cwd=res.worktree.path,
    )
    if commits.strip():
        return
    log.error("pr_refused_empty", branch=state.work_branch)
    raise RepoError(
        "refusing to open a pull request with no commits: the run reached PR without "
        "committing anything. Work in the worktree is discarded at teardown, so if the "
        "agent wrote code it has been lost — check whether it called git_commit."
    )


async def _refuse_secrets(state: RunState, deps: Deps, res: RunResources) -> None:
    """Scan for a committed secret and refuse the push if there is one.

    Run here rather than trusted from the SECURITY phase, for two reasons the phase
    document names and one it does not:

    - A fix round can introduce a secret *after* the scan that cleared the run.
    - `security_node` records an empty report when there is no sandbox — but gitleaks runs
      on the worker and needs no sandbox, so a resumed run that lost its sandbox would
      otherwise push having never been scanned for secrets at all.
    - It costs one subprocess (measured: ~100ms) immediately before the one step in this
      pipeline that cannot be undone.

    What the SECURITY phase found is folded in rather than replaced: a fresh scan that
    fails to run produces a `scan-failed` finding, which cannot gate, and a secret already
    found should not be forgotten because of it.
    """
    assert res.worktree is not None
    fresh = await scanners.run_gitleaks(res.worktree.path, state.base_sha or "HEAD")
    known = state.security.findings if state.security else []
    leaks = gitleaks_gate([*fresh, *known])
    if not leaks:
        return
    where = sorted({f"{f.file}:{f.line}" for f in leaks})
    log.error("push_refused_secret", count=len(leaks), files=where)
    # On the record before the exception, because the run is about to end and the artifact
    # is what a human will open to find out what to rotate. Safe to store: `--redact` and
    # the parser between them mean no finding here holds the value.
    async with session(deps.engine) as s:
        await db.save_artifact(
            s,
            state.run_id,
            "gitleaks",
            None,
            {"findings": [f.model_dump(mode="json") for f in leaks]},
        )
    state.error = "secrets detected"
    raise RepoError(
        f"refusing to push: {len(leaks)} secret(s) committed by this run "
        f"({', '.join(where)}). The value is withheld deliberately — rotate the credential "
        "and remove it from the branch's history before pushing."
    )


async def _push_and_open(state: RunState, deps: Deps, res: RunResources) -> str:
    """Scan, push, describe, open. Returns the pull request URL.

    The order is the design. The secret scan is before the push because a push cannot be
    undone; the push is before the writer because a branch on the remote is worth having
    even if the description fails; and the description is written from facts the harness
    already holds, so a writer that fails costs prose and nothing else.

    Shared by the two ways a run ends with a pull request — a completed one and an
    escalated one — rather than copied into each. A second copy of "scan, then push" is a
    second place for the secret gate to be forgotten, and the escalation path is the one
    less likely to be exercised in testing.
    """
    assert res.worktree is not None
    await _refuse_nothing_to_propose(state, res)
    await _refuse_secrets(state, deps, res)
    await push_branch(res.worktree.path, state.work_branch, deps.git_token())
    state.pushed = True
    if deps.github is None:
        raise RuntimeError("no GitHub client: set GITHUB_TOKEN so the run can open a pull request")

    facts = await _body_facts(state, deps, res)
    commits = await git(
        "log",
        "--oneline",
        f"--max-count={pr_writer.MAX_COMMITS}",
        f"{state.base_sha}..HEAD" if state.base_sha else "HEAD",
        cwd=res.worktree.path,
    )
    results = {task_id: r.summary for task_id, r in state.task_results.items()}

    # `state.phase`, not a literal: this also runs from ESCALATE, and a step that
    # claims to be in PR when it was not makes the audit trail lie.
    step_id, hooks, _ctx, route = await _begin(state, deps, res, "pr_writer", state.phase)
    description = None
    error: str | None = None
    try:
        description = await pr_writer.PRWriterAgent(tier=route.tier).run(
            deps.provider, facts, commits, results, hooks
        )
    except Exception as e:
        # The work is done and pushed. Losing the pull request because a prose model was
        # unavailable would be the wrong trade, so the harness writes a duller one from
        # the same facts — which is also the clearest demonstration of how little the
        # writer is trusted with.
        error = f"{type(e).__name__}: {e}"
        log.warning("pr_writer_failed", error=error, note="falling back to a generated body")
        description = pr_writer.fallback(facts)
    finally:
        await _end(state, deps, step_id, hooks, description, error, None)

    body = pr_body.render(description, facts)
    draft = pr_body.is_draft(facts)
    url = await open_pr(
        state.repo_url,
        head=state.work_branch,
        base=state.base_branch,
        title=pr_body.title(description, facts),
        body=body,
        client=deps.github,
        draft=draft,
        labels=["autoswe", "needs-review"],
    )
    async with session(deps.engine) as s:
        await db.save_artifact(
            s,
            state.run_id,
            "pr",
            url,
            {
                "description": description.model_dump(mode="json"),
                "body": body,
                "draft": draft,
                "written_by": "fallback" if error else "pr_writer",
            },
        )
    await _emit(
        deps,
        state.run_id,
        "pr_opened",
        {
            "pr_url": url,
            "draft": draft,
            "known_issues": len(state.known_issues),
            "written_by": "fallback" if error else "pr_writer",
            "escalated": bool(state.escalation_reason),
        },
    )
    return url


async def pr_node(state: RunState, deps: Deps, res: RunResources) -> RunState:
    """The completed path: a run that reached PR with its work done."""
    state.pr_url = await _push_and_open(state, deps, res)
    async with session(deps.engine) as s:
        await db.finish_run(s, state.run_id, status="done", pr_url=state.pr_url)
    return state


NODES: dict[Phase, Node] = {
    Phase.SETUP: setup_node,
    Phase.ANALYZE: analyze_node,
    Phase.PLAN: plan_node,
    Phase.DECOMPOSE: decompose_node,
    Phase.AWAITING_INPUT: awaiting_input_node,
    Phase.CODE: code_node,
    Phase.DEBUG: debug_node,
    Phase.ESCALATE: escalate_node,
    Phase.TEST: test_node,
    Phase.REVIEW: review_node,
    Phase.SECURITY: security_node,
    Phase.PR: pr_node,
}


async def teardown(state: RunState, deps: Deps, res: RunResources) -> None:
    """Always runs. Never raises: a teardown failure must not mask the real error."""
    for task in (res.renewer, res.embedding_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
