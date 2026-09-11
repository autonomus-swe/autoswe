"""One place that publishes a run event to both the live stream and the durable table.

Payloads stay small: a viewer needs to know what happened, not to re-read it. The full
content lives in ``tool_calls`` and ``artifacts``.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from storage import repo as db
from storage.db import session
from storage.redis import RedisBus

MAX_INPUT_CHARS = 2048
MAX_OUTPUT_CHARS = 1024


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"… (+{len(value) - limit} chars)"
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    return value


def shrink(payload: dict[str, Any]) -> dict[str, Any]:
    """Cap the two fields that can carry a whole file."""
    out = dict(payload)
    if "input" in out:
        out["input"] = _truncate(out["input"], MAX_INPUT_CHARS)
    if "output" in out:
        out["output"] = _truncate(out["output"], MAX_OUTPUT_CHARS)
    return out


async def emit(
    bus: RedisBus | None,
    engine: AsyncEngine | None,
    run_id: UUID,
    type: str,
    payload: dict[str, Any],
) -> None:
    """Record the event, then publish it. The table is the source of truth for replay."""
    body = shrink(payload)
    if engine is not None:
        async with session(engine) as s:
            await db.insert_event(s, run_id, type, body)
    if bus is not None:
        # a dead stream must not fail a run; the event is already durable
        with contextlib.suppress(Exception):
            await bus.emit(run_id, type, body)
