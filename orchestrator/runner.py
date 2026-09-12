"""Drive the phase machine, checkpointing after every node so a crash costs one node."""

from __future__ import annotations

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


async def _cancelled(state: RunState, deps: Deps) -> bool:
    if state.cancelled or await deps.bus.is_cancelled(state.run_id):
        state.cancelled = True
        return True
    return False


async def run(state: RunState, deps: Deps, res: RunResources | None = None) -> RunState:
    """Run to a terminal phase. ``res`` carries infrastructure a resume re-attached."""
    res = res or RunResources()
    bind_run(state.run_id)
    try:
        async with session(deps.engine) as s:
            await db.mark_run_started(s, state.run_id)
        while state.phase not in TERMINAL:
            if await _cancelled(state, deps):
                state.phase, state.error = Phase.FAILED, "cancelled"
                break
            log.info("phase_start", phase=state.phase.value)
            with trace_span(f"phase.{state.phase.value}", run_id=str(state.run_id)):
                state = await NODES[state.phase](state, deps, res)
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
                await db.finish_run(s, state.run_id, status=status, error=state.error)
            await _finished(deps, state, status)
        elif state.phase is Phase.DONE:
            await _finished(deps, state, "done")
    except RunCancelled as e:
        # a human asked for this, so it is not a failure and must not be retried
        state.cancelled = True
        state.phase, state.error = Phase.FAILED, str(e) or "cancelled"
        log.info("run_cancelled", reason=state.error)
        async with session(deps.engine) as s:
            await db.finish_run(s, state.run_id, status="cancelled", error=state.error)
        await _finished(deps, state, "cancelled")
    except Exception as e:
        state.phase, state.error = Phase.FAILED, f"{type(e).__name__}: {e}"
        log.error("run_failed", error=state.error)
        async with session(deps.engine) as s:
            await db.finish_run(s, state.run_id, status="failed", error=state.error)
        raise
    finally:
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
