"""Redis is the nervous system (README §4.8): event streams, human inbox, locks, rate limits,
cancel flags. Check-and-set operations are Lua scripts so they are atomic."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable
from typing import Any

import orjson
import redis.asyncio as aioredis

BASELINE_TTL_S = 7 * 24 * 3600

_RENEW_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_BUCKET_SCRIPT = """
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * refill)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / refill * 1000) + 1000)
return allowed
"""


async def _aw[T](value: Awaitable[T] | T) -> T:
    """redis-py types many commands as ``Awaitable[T] | T``; normalise to T."""
    if isinstance(value, Awaitable):
        return await value
    return value


def events_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:events"


def inbox_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:inbox"


def pending_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:pending"


def cancel_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:cancel"


def baseline_key(repo: str, sha: str) -> str:
    """Which tests were already failing at a commit — a property of the commit, not of
    the run that discovered it, which is why this is not in the run's artifacts."""
    return f"baseline:{repo}:{sha}"


class RedisBus:
    def __init__(self, url: str, *, stream_maxlen: int = 10_000) -> None:
        self.r: aioredis.Redis = aioredis.from_url(url, decode_responses=True)  # type: ignore[no-untyped-call]
        self.stream_maxlen = stream_maxlen
        self._renew = self.r.register_script(_RENEW_LOCK)
        self._release = self.r.register_script(_RELEASE_LOCK)
        self._take = self.r.register_script(_BUCKET_SCRIPT)

    async def close(self) -> None:
        await self.r.aclose()

    async def ping(self) -> bool:
        return bool(await self.r.ping())

    # ---- events: append-only stream per run --------------------------------

    async def emit(self, run_id: uuid.UUID | str, type: str, payload: dict[str, Any]) -> str:
        entry_id = await self.r.xadd(
            events_key(run_id),
            {"type": type, "payload": orjson.dumps(payload).decode()},
            maxlen=self.stream_maxlen,
            approximate=True,
        )
        return str(entry_id)

    async def read_events(
        self,
        run_id: uuid.UUID | str,
        last_id: str = "0-0",
        block_ms: int = 15_000,
        count: int = 100,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Entries after ``last_id``. Blocks up to ``block_ms``; empty list on timeout."""
        res = await self.r.xread({events_key(run_id): last_id}, count=count, block=block_ms)
        out: list[tuple[str, dict[str, Any]]] = []
        for _stream, entries in res or []:
            for entry_id, fields in entries:
                out.append(
                    (
                        str(entry_id),
                        {"type": fields["type"], "payload": orjson.loads(fields["payload"])},
                    )
                )
        return out

    # ---- human-in-the-loop inbox --------------------------------------------

    async def push_inbox(self, run_id: uuid.UUID | str, message: dict[str, Any]) -> None:
        await _aw(self.r.rpush(inbox_key(run_id), orjson.dumps(message).decode()))

    async def pop_inbox(self, run_id: uuid.UUID | str, timeout_s: int) -> dict[str, Any] | None:
        res = await _aw(self.r.blpop([inbox_key(run_id)], timeout=timeout_s))
        if res is None:
            return None
        _key, raw = res
        data: dict[str, Any] = orjson.loads(raw)
        return data

    # ---- locks: one run per repo+branch -------------------------------------

    async def acquire_lock(self, key: str, owner: str, ttl_s: int) -> bool:
        return bool(await self.r.set(key, owner, nx=True, px=ttl_s * 1000))

    async def renew_lock(self, key: str, owner: str, ttl_s: int) -> bool:
        return bool(await self._renew(keys=[key], args=[owner, ttl_s * 1000]))

    async def release_lock(self, key: str, owner: str) -> bool:
        return bool(await self._release(keys=[key], args=[owner]))

    async def lock_owner(self, key: str) -> str | None:
        val = await self.r.get(key)
        return str(val) if val is not None else None

    # ---- token bucket per API key -------------------------------------------

    async def take_token(self, bucket: str, capacity: int, refill_per_s: float) -> bool:
        now_ms = int(time.time() * 1000)
        return bool(await self._take(keys=[bucket], args=[capacity, refill_per_s, now_ms]))

    # ---- pending approval ----------------------------------------------------
    # The API process validates an approval against this and never sees RunState, so the
    # id of the call being waited on has to live somewhere both processes can reach.

    async def set_pending(self, run_id: uuid.UUID | str, tool_call_id: str) -> None:
        await self.r.set(pending_key(run_id), tool_call_id, ex=24 * 3600)

    async def get_pending(self, run_id: uuid.UUID | str) -> str | None:
        val = await self.r.get(pending_key(run_id))
        return str(val) if val is not None else None

    async def clear_pending(self, run_id: uuid.UUID | str) -> None:
        await self.r.delete(pending_key(run_id))

    # ---- cancel flag ---------------------------------------------------------

    async def set_cancel(self, run_id: uuid.UUID | str, ttl_s: int = 24 * 3600) -> None:
        await self.r.set(cancel_key(run_id), "1", ex=ttl_s)

    async def is_cancelled(self, run_id: uuid.UUID | str) -> bool:
        return int(await self.r.exists(cancel_key(run_id))) == 1

    # ---- baseline cache ------------------------------------------------------
    # Purely a cache: a miss costs one full suite run, so a cold or evicted Redis is
    # slower and never wrong. The TTL exists because the same commit can produce a
    # different baseline once its dependencies resolve differently.

    async def set_baseline(
        self, repo: str, sha: str, signatures: list[str], ttl_s: int = BASELINE_TTL_S
    ) -> None:
        await self.r.set(
            baseline_key(repo, sha), orjson.dumps({"signatures": signatures}).decode(), ex=ttl_s
        )

    async def get_baseline(self, repo: str, sha: str) -> list[str] | None:
        """The cached signatures, or None on a miss. An empty list is a hit: a repository
        whose suite is green has a baseline, and it is nothing."""
        raw = await _aw(self.r.get(baseline_key(repo, sha)))
        if raw is None:
            return None
        try:
            data = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None
        signatures = data.get("signatures") if isinstance(data, dict) else None
        return [str(s) for s in signatures] if isinstance(signatures, list) else None
