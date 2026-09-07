"""Liveness and readiness. No authentication: these must work when keys are misconfigured."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from storage.db import session

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request, response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}
    try:
        async with session(request.app.state.engine) as s:
            await s.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {type(e).__name__}"
    try:
        checks["redis"] = "ok" if await request.app.state.bus.ping() else "error"
    except Exception as e:
        checks["redis"] = f"error: {type(e).__name__}"
    ok = all(v == "ok" for v in checks.values())
    response.status_code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "checks": checks}


@router.get("/metrics")
async def metrics() -> dict[str, str]:
    return {"detail": "prometheus metrics arrive in Phase 5"}
