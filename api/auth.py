"""API-key authentication and a Redis token bucket per key."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from core.settings import Settings, get_settings

RATE_CAPACITY = 5
RATE_REFILL_PER_S = 5 / 60
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

UNAUTHORIZED = HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
RATE_LIMITED = HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")


def _matches(candidate: str, configured: frozenset[str]) -> bool:
    # compare_digest against every key so the time taken does not reveal which key matched
    return any(hmac.compare_digest(candidate, known) for known in configured)


async def require_api_key(
    key: str | None = Depends(api_key_header),
    settings: Settings = Depends(get_settings),
) -> str:
    if not key or not _matches(key, settings.api_keys):
        raise UNAUTHORIZED
    return key


async def rate_limit(request: Request, key: str = Depends(require_api_key)) -> str:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return key
    bucket = f"rl:{hashlib.sha256(key.encode()).hexdigest()}"
    if not await bus.take_token(bucket, RATE_CAPACITY, RATE_REFILL_PER_S):
        raise RATE_LIMITED
    return key
