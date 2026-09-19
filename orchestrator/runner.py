"""Drive the phase machine, checkpointing after every node so a crash costs one node."""

from __future__ import annotations

import asyncio
import contextlib

from core.errors import RunCancelled
from observability.logging import bind_run, clear_run, get_logger
from observability.tracing import trace_span
from orchestrator.checkpoint import save
from orchestrator.deps import Deps
from orchestrator.nodes import NODES, RunResources, teardown
from orchestrator.state import TERMINAL, Phase, RunState, status_for
from orchestrator.transition import transition
from storage import repo as db
from storage.db import session

log = get_logger(__name__)


CANCEL_POLL_S = 2.0


async def _cancelled(state: RunState, deps: Deps) -> bool:
    if state.cancelled or await deps.bus.is_cancelled(state.run_id):
        state.cancelled = True
        return True
    return False


async def _watch_for_cancel(state: RunState, deps: Deps, res: RunResources) -> None:
    """Notice a cancel *while* a phase is running, and stop what it is running.

    The checks around each node are not enough on their own: a phase spends most of its
    time inside one command — an install, a test suite — and a human who asks a run to
    stop should not wait ten minutes for a suite they no longer care about. `before_tool`
    catches a cancel between tool calls; this catches one during a call.

    It never ends the run itself. It sets the flag and stops the sandbox; the runner reads
    the flag and decides, so there is still exactly one place that ends a run.
    """
    while True:
        await asyncio.sleep(CANCEL_POLL_S)
        if not await deps.bus.is_cancelled(state.run_id):
            continue
        state.cancelled = True
        if res.sandbox is not None:
            log.info("cancel_killing_sandbox", phase=state.phase.value)
            with contextlib.suppress(Exception):  # a failed kill must not mask the cancel
                await res.sandbox.kill_exec()
        return


async def run(state: RunState, deps: Deps, res: RunResources | None = None) -> RunState:
    """Run to a terminal phase. ``res`` carries infrastructure a resume re-attached."""
    res = res or RunResources()
    bind_run(state.run_id)
    watcher = asyncio.create_task(_watch_for_cancel(state, deps, res))
    # The root span, so everything below it is one tree rather than a phase's worth of
    # orphans. It wraps the loop rather than sitting inside it, so it still closes when a
    # phase raises — the traces worth reading are mostly those.
    with trace_span(
        "run",
        run_id=str(state.run_id),
        repo=state.repo_url,
        provider=deps.provider.provider_name,
        model=deps.provider.model,
        goal=state.goal[:200],
    ):
        return await _run_phases(state, deps, res, watcher)


async def _run_phases(
    state: RunState, deps: Deps, res: RunResources, watcher: asyncio.Task[None]
) -> RunState:
    """The phase loop. Split from `run` only so the root span wraps it without indenting
    two hundred lines."""
    try:
        async with session(deps.engine) as s:
            await db.mark_run_started(s, state.run_id)
        while state.phase not in TERMINAL:
            if await _cancelled(state, deps):
                state.phase, state.error = Phase.FAILED, "cancelled"
                break
            log.info("phase_start", phase=state.phase.value)
            with trace_span(f"phase.{state.phase.value}", run_id=str(state.run_id)):
                try:
                    state = await NODES[state.phase](state, deps, res)
                except Exception as e:
                    # A node that blew up because its sandbox was killed under it did not
                    # fail: it was stopped. Anything else is a real failure.
                    if state.cancelled:
                        raise RunCancelled("cancelled while a phase was running") from e
                    raise
            await save(deps.engine, state)  # the node's work is durable before we move on

            if await _cancelled(state, deps):
                state.phase, state.error = Phase.FAILED, "cancelled"
                break
            if state.phase not in TERMINAL:
                state.phase = transition(state)
            await save(deps.engine, state)
            async with session(deps.engine) as s:
                await db.set_run_phase(s, state.run_id, state.phase.value, status_for(state.phase))

        if state.phase is Phase.FAILED:
            status = "cancelled" if state.cancelled else "failed"
            state.error = state.error or "a phase produced no usable result"
            async with session(deps.engine) as s:
                # `pr_url` is passed even though the status is not "done": an escalated run
                # with commits opens a draft, and omitting it here would write NULL over
                # the URL that path just set — losing the only pointer to the work.
                await db.finish_run(
                    s, state.run_id, status=status, pr_url=state.pr_url, error=state.error
                )
            await _finished(deps, state, status)
        elif state.phase is Phase.DONE:
            await _finished(deps, state, "done")
    except RunCancelled as e:
        # a human asked for this, so it is not a failure and must not be retried
        state.cancelled = True
        state.phase, state.error = Phase.FAILED, str(e) or "cancelled"
        log.info("run_cancelled", reason=state.error)
        async with session(deps.engine) as s:
            await db.finish_run(
                s, state.run_id, status="cancelled", pr_url=state.pr_url, error=state.error
            )
        await _finished(deps, state, "cancelled")
    except Exception as e:
        state.phase, state.error = Phase.FAILED, f"{type(e).__name__}: {e}"
        log.error("run_failed", error=state.error)
        async with session(deps.engine) as s:
            await db.finish_run(
                s, state.run_id, status="failed", pr_url=state.pr_url, error=state.error
            )
        raise
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await teardown(state, deps, res)
        clear_run()
    return state


async def _finished(deps: Deps, state: RunState, status: str) -> None:
    from orchestrator.nodes import _emit

    await _emit(
        deps,
        state.run_id,
        "run_finished",
        {
            "status": status,
            "pr_url": state.pr_url,
            "error": state.error,
            "cost_usd": round(state.usage.cost_usd, 6),
        },
    )
