"""Drive the phase machine to a terminal phase. v1 has no checkpoints (Phase 2)."""

from __future__ import annotations

from observability.logging import bind_run, clear_run, get_logger
from observability.tracing import trace_span
from orchestrator.deps import Deps
from orchestrator.nodes import NODES, RunResources, teardown
from orchestrator.state import TERMINAL, Phase, RunState, status_for
from orchestrator.transition import transition
from storage import repo as db
from storage.db import session

log = get_logger(__name__)


async def run(state: RunState, deps: Deps) -> RunState:
    res = RunResources()
    bind_run(state.run_id)
    try:
        async with session(deps.engine) as s:
            await db.mark_run_started(s, state.run_id)
        while state.phase not in TERMINAL:
            log.info("phase_start", phase=state.phase.value)
            with trace_span(f"phase.{state.phase.value}", run_id=str(state.run_id)):
                state = await NODES[state.phase](state, deps, res)
            state.phase = transition(state)
            async with session(deps.engine) as s:
                await db.set_run_phase(s, state.run_id, state.phase.value, status_for(state.phase))
        if state.phase is Phase.FAILED and state.error is None:
            state.error = "a phase produced no usable result"
            async with session(deps.engine) as s:
                await db.finish_run(s, state.run_id, status="failed", error=state.error)
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
