from __future__ import annotations

import asyncio
import uuid

import pytest

from storage.redis import RedisBus

pytestmark = pytest.mark.integration


async def test_ping(bus: RedisBus) -> None:
    assert await bus.ping()


async def test_event_stream_append_and_replay(bus: RedisBus) -> None:
    run_id = uuid.uuid4()
    ids = [
        await bus.emit(run_id, "phase_changed", {"phase": p}) for p in ("setup", "analyze", "plan")
    ]
    all_events = await bus.read_events(run_id, "0-0", block_ms=100)
    assert [e["payload"]["phase"] for _, e in all_events] == ["setup", "analyze", "plan"]
    assert [i for i, _ in all_events] == ids
    later = await bus.read_events(run_id, ids[0], block_ms=100)
    assert [e["payload"]["phase"] for _, e in later] == ["analyze", "plan"]
    assert await bus.read_events(run_id, ids[-1], block_ms=100) == []


async def test_inbox_blocks_then_delivers(bus: RedisBus) -> None:
    run_id = uuid.uuid4()
    assert await bus.pop_inbox(run_id, timeout_s=1) is None

    async def answer_later() -> None:
        await asyncio.sleep(0.2)
        await bus.push_inbox(run_id, {"type": "answer", "text": "JWT"})

    task = asyncio.create_task(answer_later())
    msg = await bus.pop_inbox(run_id, timeout_s=5)
    await task
    assert msg == {"type": "answer", "text": "JWT"}


async def test_lock_is_exclusive_and_owner_checked(bus: RedisBus) -> None:
    key = "lock:repo:abc:main"
    assert await bus.acquire_lock(key, "worker-A", ttl_s=5)
    assert not await bus.acquire_lock(key, "worker-B", ttl_s=5)
    assert not await bus.renew_lock(key, "worker-B", ttl_s=5)
    assert await bus.renew_lock(key, "worker-A", ttl_s=5)
    assert not await bus.release_lock(key, "worker-B")
    assert await bus.lock_owner(key) == "worker-A"
    assert await bus.release_lock(key, "worker-A")
    assert await bus.acquire_lock(key, "worker-B", ttl_s=5)


async def test_lock_expires_without_renewal(bus: RedisBus) -> None:
    key = "lock:expiring"
    assert await bus.acquire_lock(key, "A", ttl_s=1)
    await asyncio.sleep(1.2)
    assert await bus.lock_owner(key) is None


async def test_token_bucket_allows_capacity_then_refills(bus: RedisBus) -> None:
    bucket = "rl:test"
    results = [await bus.take_token(bucket, capacity=3, refill_per_s=100.0) for _ in range(4)]
    assert results == [True, True, True, False]
    await asyncio.sleep(0.05)
    assert await bus.take_token(bucket, capacity=3, refill_per_s=100.0)


async def test_cancel_flag(bus: RedisBus) -> None:
    run_id = uuid.uuid4()
    assert not await bus.is_cancelled(run_id)
    await bus.set_cancel(run_id)
    assert await bus.is_cancelled(run_id)
