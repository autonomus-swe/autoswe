"""Server-sent events for a run: live while it is going, replayed once it is over."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import orjson
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

from api.auth import require_api_key_or_query
from observability.logging import get_logger
from storage import repo as db
from storage.db import session

log = get_logger(__name__)
router = APIRouter(prefix="/runs", tags=["events"])

BLOCK_MS = 15_000
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


def _frame(event_id: str, type: str, payload: dict[str, Any]) -> dict[str, str]:
    return {"id": event_id, "event": type, "data": orjson.dumps(payload).decode()}


async def _replay(engine: Any, run_id: UUID, after_id: int) -> list[dict[str, str]]:
    async with session(engine) as s:
        rows = await db.list_events(s, run_id, after_id=after_id)
    return [_frame(str(r.id), r.type, r.payload) for r in rows]


@router.get("/{run_id}/events")
async def stream_events(
    run_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _key: str = Depends(require_api_key_or_query),
) -> EventSourceResponse:
    """Live events, or the whole history if the run has already finished.

    Reconnect with ``Last-Event-ID`` and you get only what you missed. A finished run is
    replayed from the events table and the stream closes; the Redis stream is capped and
    is not the source of truth.
    """
    engine = request.app.state.engine
    async with session(engine) as s:
        row = await db.get_run(s, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")

    bus = request.app.state.bus
    terminal = row.status in TERMINAL_STATUSES

    async def source() -> AsyncIterator[dict[str, str]]:
        if terminal:
            for frame in await _replay(engine, run_id, _int_or_zero(last_event_id)):
                yield frame
            return

        # a reconnect resumes from the caller's cursor; a fresh viewer gets the history
        # so far, then follows the live stream from that point
        history = await _replay(engine, run_id, _int_or_zero(last_event_id))
        for frame in history:
            yield frame

        cursor = "0-0" if not history else "$"
        while not await request.is_disconnected():
            entries = await bus.read_events(run_id, cursor, block_ms=BLOCK_MS)
            if not entries:
                yield {"event": "keepalive", "data": ""}
                async with session(engine) as s:
                    current = await db.get_run(s, run_id)
                if current is not None and current.status in TERMINAL_STATUSES:
                    return
                continue
            for entry_id, event in entries:
                cursor = entry_id
                yield _frame(entry_id, str(event["type"]), dict(event["payload"]))
                if event["type"] == "run_finished":
                    return
            await asyncio.sleep(0)

    return EventSourceResponse(source(), headers={"X-Accel-Buffering": "no"})


def _int_or_zero(value: str | None) -> int:
    try:
        return int(value) if value else 0
    except ValueError:
        return 0
