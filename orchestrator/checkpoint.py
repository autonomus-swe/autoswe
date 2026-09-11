"""Durable run state: one row per node executed, so a crash costs at most one node."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from observability.logging import get_logger
from orchestrator.state import RunState
from storage import repo as db
from storage.db import session

log = get_logger(__name__)


async def save(engine: Any, state: RunState) -> int:
    """Persist the state and return its sequence number."""
    state.seq += 1
    async with session(engine) as s:
        await db.save_checkpoint(
            s, state.run_id, state.seq, state.phase.value, state.model_dump(mode="json")
        )
    return state.seq


async def load_latest(engine: Any, run_id: UUID) -> RunState | None:
    """The most recent checkpoint, or None for a run that never reached one."""
    async with session(engine) as s:
        row = await db.latest_checkpoint(s, run_id)
    if row is None:
        return None
    seq, payload = row
    state = RunState.model_validate(payload)
    log.info("checkpoint_loaded", run_id=str(run_id), seq=seq, phase=state.phase.value)
    return state
