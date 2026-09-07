"""v1: accounting only. Enforcement (stop the run when over budget) arrives in Phase 3."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from contracts import Usage
from storage import repo


async def record_llm_call(
    s: AsyncSession,
    *,
    step_id: UUID,
    provider: str,
    model: str,
    effort: str | None,
    usage: Usage,
    latency_ms: int,
    stop_reason: str | None,
) -> UUID:
    return await repo.insert_llm_call(
        s,
        step_id=step_id,
        provider=provider,
        model=model,
        effort=effort,
        usage=usage,
        latency_ms=latency_ms,
        stop_reason=stop_reason,
    )
