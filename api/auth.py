"""API-key authentication and a Redis token bucket per key."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from core.settings import Settings, get_settings

# Starting a run spends money, so the write bucket is deliberately small. Reads are
# cheap and a console legitimately re-reads a run on every phase change, so they get
# their own, far larger bucket; sharing the write bucket made the UI throttle itself.
RATE_CAPACITY = 5
RATE_REFILL_PER_S = 5 / 60
READ_RATE_CAPACITY = 120
READ_RATE_REFILL_PER_S = 2.0
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


async def _take(request: Request, key: str, prefix: str, capacity: int, refill: float) -> str:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return key
    bucket = f"{prefix}:{hashlib.sha256(key.encode()).hexdigest()}"
    if not await bus.take_token(bucket, capacity, refill):
        raise RATE_LIMITED
    return key


async def rate_limit(request: Request, key: str = Depends(require_api_key)) -> str:
    """The bucket for anything that changes state."""
    return await _take(request, key, "rl", RATE_CAPACITY, RATE_REFILL_PER_S)


async def read_rate_limit(request: Request, key: str = Depends(require_api_key)) -> str:
    """A separate, looser bucket so polling a run never blocks starting one."""
    return await _take(request, key, "rlr", READ_RATE_CAPACITY, READ_RATE_REFILL_PER_S)


async def require_api_key_or_query(
    request: Request,
    key: str | None = Depends(api_key_header),
    settings: Settings = Depends(get_settings),
) -> str:
    """Accept the key as a query parameter as well as a header.

    Browsers cannot set headers on an ``EventSource``, so the stream endpoint has no
    other way to authenticate one. Query strings end up in access logs and history, so
    this is deliberately limited to the read-only event stream; every other route
    requires the header.
    """
    candidate = key or request.query_params.get("key")
    if not candidate or not _matches(candidate, settings.api_keys):
        raise UNAUTHORIZED
    return candidate
