"""Writes and reads are rate limited against two separate buckets, and the separation is
one string: the key prefix `rlr` that `read_rate_limit` hands to `_take`.

Changing it to `rl` — the write prefix — survived the whole suite:

| mutation | consequence |
|---|---|
| `"rlr"` → `"rl"` in `read_rate_limit` | **one ledger for both, so neither limit limits** |

Two tests looked like they covered this and did not. `test_rate_limit_after_the_burst`
makes six writes and no reads, so a shared bucket behaves exactly like the write bucket.
`test_reads_do_not_spend_the_write_budget` makes twenty reads and then one write — but the
bucket script clamps with the *caller's* capacity (`math.min(capacity, ...)`), so twenty
reads leave a shared bucket at around a hundred tokens, and the write that follows clamps
that straight back down to its own capacity of five and succeeds. The test passes while
measuring nothing. Nothing in the suite interleaves a read with a write burst.

The two things one shared ledger actually does:

**A write burst blinds the console.** Five writes empty the shared bucket, and reads are
then refused one after another — they refill at 2/s, so a console polling a run through its
phases goes dark because somebody else started five runs. Its own budget is 120 and
untouched.

**A read hands the write budget back.** A shared bucket above one token — which a read's
2/s refill produces in a second — is a write allowance, because the write clamps to five
and takes one. Any caller that interleaves a GET can start runs forever. That is worse
than two limits merging: it is the small limit being erased by the traffic the large one
was sized for.

Against a real Redis because the budget lives in a Lua script, and the clamp inside it is
the entire reason the existing tests pass.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from storage.redis import RedisBus
from tests.integration.conftest import api_app

pytestmark = pytest.mark.integration

KEY = "test-key-123456"
HEADERS = {"X-API-Key": KEY}
MISSING = f"{'0' * 8}-0000-0000-0000-{'0' * 12}"

# api/auth.py: the write bucket holds 5 and refills at 5/60 per second, so one spent token
# is twelve seconds from coming back. Every pause below is sized against that — long enough
# for the read bucket's 2/s to matter, far too short for the write bucket to notice.
WRITE_CAPACITY = 5
READ_CAPACITY = 120
# More reads than the write bucket could ever allow, and nothing like the read capacity, so
# a count here cannot be mistaken for either limit.
POLLS = 15


async def a_write(client: httpx.AsyncClient) -> int:
    """One state-changing request that spends a write token and changes nothing.

    Cancelling a run that does not exist costs a token and returns 404, which is the
    separation these tests need: the budget moves, the database does not. 429 therefore
    means the limiter refused, and 404 means it did not.
    """
    return (await client.post(f"/runs/{MISSING}/cancel", headers=HEADERS)).status_code


async def a_read(client: httpx.AsyncClient) -> int:
    """One read, of the shape a console repeats on every phase change."""
    return (await client.get(f"/runs/{MISSING}", headers=HEADERS)).status_code


async def tokens_left(bus: RedisBus, prefix: str) -> float | None:
    """What one budget has left, read off its own ledger. `None` when there is no ledger.

    Under the mutation `None` is what the read side returns, because the only key that ever
    existed is the write one.
    """
    keys = [str(k) async for k in bus.r.scan_iter(f"{prefix}:*")]
    assert len(keys) <= 1, f"one API key should own one {prefix!r} bucket, found {keys}"
    if not keys:
        return None
    return float(await bus.r.hget(keys[0], "tokens"))  # type: ignore[misc]


async def spend_the_write_budget(client: httpx.AsyncClient) -> None:
    """Leave the write bucket empty, having checked it was full to begin with.

    The check matters: if the limiter refused from the first request this whole file would
    still be green on "a write is refused", which is the assertion a broken limiter passes.
    """
    allowed = [await a_write(client) for _ in range(WRITE_CAPACITY)]
    assert allowed == [404] * WRITE_CAPACITY, f"a full write bucket refused a write: {allowed}"
    assert await a_write(client) == 429, "the write bucket did not run out after five writes"


async def test_a_burst_of_writes_does_not_blind_the_console(engine: AsyncEngine) -> None:
    """Somebody starts five runs; everybody else's console stops rendering.

    With one shared ledger the write burst empties it, and reads are then refused at the
    read refill rate — about one allowed every half second against a budget of 120 that was
    never spent. The console shows a run stuck in whatever phase it was in when the burst
    happened, and the only symptom is 429s that the user did not earn.

    This is the interleaving nobody tested: writes first, then reads.
    """
    async with api_app(engine) as (client, _arq):
        await spend_the_write_budget(client)

        polls = [await a_read(client) for _ in range(POLLS)]

        assert polls == [404] * POLLS, (
            f"reads were refused after a write burst ({polls.count(429)} of {POLLS} got 429); "
            "the console pays for somebody else's runs, so the two budgets are one"
        )


async def test_a_read_does_not_hand_the_write_budget_back(engine: AsyncEngine) -> None:
    """The write limit stops limiting if a GET can top it up.

    Starting a run spends money, which is the whole reason the write bucket holds five. On
    a shared ledger a single read a second later leaves it above one token — the read side
    refills at 2/s — and the next write clamps that to its own capacity and is allowed. A
    caller polling between writes then has no write limit at all.

    The pause is 1.5s: three read-tokens, and an eighth of the twelve seconds the write
    bucket needs for one. A write allowed after it can only have been paid for by the read
    budget.
    """
    async with api_app(engine) as (client, _arq):
        await spend_the_write_budget(client)

        await asyncio.sleep(1.5)
        assert await a_read(client) == 404, "the read budget was untouched and should serve"

        assert await a_write(client) == 429, (
            "a write was allowed 1.5s into a 12s refill, so the read paid for it"
        )


async def test_the_console_budget_is_a_second_ledger_and_not_the_write_one(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    """Read straight off Redis, because the two balances are the claim.

    After the write burst the write ledger is empty and the read ledger is three off full.
    One number cannot be both, so a single shared key fails here before any behaviour is
    involved — and it fails by reporting that the read bucket does not exist, which is the
    mutation stated plainly.

    The upper bound is the counterweight: a read limiter that took no token at all would
    leave the ledger at capacity, and "reads are never refused" is not the claim either.
    """
    async with api_app(engine) as (client, _arq):
        await spend_the_write_budget(client)
        polls = [await a_read(client) for _ in range(3)]
        assert polls == [404] * 3, f"reads refused while their own budget was full: {polls}"

        write_budget = await tokens_left(bus, "rl")
        read_budget = await tokens_left(bus, "rlr")

    assert read_budget is not None, (
        "no 'rlr' bucket exists: reads are being charged to the write ledger"
    )
    assert write_budget is not None and write_budget < 1.0, (
        f"the write budget should be spent, found {write_budget}"
    )
    assert read_budget > READ_CAPACITY - WRITE_CAPACITY - 3, (
        f"the read budget paid for the writes, found {read_budget}"
    )
    assert read_budget <= READ_CAPACITY - 1, (
        f"three reads cost the read budget nothing, found {read_budget}"
    )
