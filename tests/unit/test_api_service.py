"""The transport-neutral control plane, tested without a transport.

`api/service.py` exists for one stated reason: the approval replay guard "lives here rather
than in a transport so that every way of approving goes through it — the reason this module
exists at all". It had **no test file**. It reached 97 % coverage incidentally, through HTTP
route tests and MCP server tests that happen to pass through it, and the lines those two
routes never take are exactly the ones where the guarantees live.

Four mutations survived the whole suite:

| mutation | consequence |
|---|---|
| `_pending`'s `raise Conflict` → `pass` | **an approval is accepted by a run not awaiting one** |
| `answer`'s `tool_call_id` → `None` | the answer cannot be matched to its question |
| `min(max(limit, 1), MAX_ROWS)` → `max(limit, 1)` | a caller asking for a million rows gets them |
| the arq-unavailable branch → `raise` | a queue outage rejects runs instead of queuing them |

The first is the one this module was written for. `_pending` is the guard that stops an
approval being replayed later against a different tool call — "which is how a human ends up
authorising something they never saw". Disarming it left the suite green.

Tested against a fake bus and a fake session rather than Postgres, because every one of
these is a decision the plane makes before it touches storage, and a test that needed a
container to check a comparison would not be run often enough to matter.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from api.service import MAX_ROWS, Conflict, ControlPlane, NotFound
from contracts import Budget

pytestmark = pytest.mark.unit

RUN_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


@dataclass
class FakeRow:
    id: uuid.UUID = RUN_ID
    status: str = "awaiting_input"
    phase: str = "code"
    work_branch: str = f"agent/{RUN_ID}"


@dataclass
class FakeBus:
    """Records what was pushed, and answers `get_pending` with whatever the test set."""

    pending: str | None = None
    inbox: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[uuid.UUID] = field(default_factory=list)

    async def push_inbox(self, run_id: uuid.UUID, message: dict[str, Any]) -> None:
        self.inbox.append(message)

    async def get_pending(self, run_id: uuid.UUID) -> str | None:
        return self.pending

    async def set_cancel(self, run_id: uuid.UUID) -> None:
        self.cancelled.append(run_id)


@dataclass
class FakeArq:
    jobs: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((name, args))


# So that `row=None` can mean "no such run" while an unspecified `row` means the ordinary
# one. Conflating the two made five tests here raise NotFound from the wrong place.
UNSET: Any = object()


def plane(
    monkeypatch: pytest.MonkeyPatch,
    *,
    row: Any = UNSET,
    bus: FakeBus | None = None,
    arq: Any = None,
    rows: list[Any] | None = None,
    created: dict[str, Any] | None = None,
) -> tuple[ControlPlane, FakeBus]:
    """A plane whose storage is a recording stub.

    `session` and the `db` functions are patched on `api.service` rather than a real engine
    being stood up: everything asserted below happens before or after storage, and the
    limit clamp in particular is a comparison this plane makes on the way *in*.
    """
    from api import service as svc

    the_bus = bus if bus is not None else FakeBus()
    seen: dict[str, Any] = created if created is not None else {}

    @asynccontextmanager
    async def fake_session(engine: Any) -> Any:
        yield object()

    async def fake_get_run(s: Any, run_id: uuid.UUID) -> Any:
        return FakeRow() if row is UNSET else row

    async def fake_list_runs(s: Any, *, limit: int) -> list[Any]:
        seen["limit"] = limit
        return rows or []

    async def fake_create_run(s: Any, **kwargs: Any) -> uuid.UUID:
        seen.update(kwargs)
        return RUN_ID

    monkeypatch.setattr(svc, "session", fake_session)
    monkeypatch.setattr("api.service.db.get_run", fake_get_run)
    monkeypatch.setattr("api.service.db.list_runs", fake_list_runs)
    monkeypatch.setattr("api.service.db.create_run", fake_create_run)
    return ControlPlane(engine=object(), bus=the_bus, arq=arq), the_bus


# ---- the replay guard: the reason this module exists ---------------------------------------


async def test_an_approval_is_refused_when_the_run_is_not_waiting_for_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard the module's own docstring names as its purpose.

    A run that has moved on is not waiting for a decision, and accepting one anyway is how
    an approval gets replayed against a call the human never saw. Replacing the `raise`
    with `pass` left the whole suite green.
    """
    p, bus = plane(monkeypatch, row=FakeRow(status="running"), bus=FakeBus(pending="call-1"))

    with pytest.raises(Conflict, match="not awaiting a decision"):
        await p.approve(RUN_ID, "call-1")

    assert bus.inbox == [], "nothing may reach the run when the guard refuses"


async def test_a_rejection_goes_through_the_same_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both decisions, because `approve` and `reject` each call `_pending` and a guard
    applied to one of two paths is not a guard."""
    p, bus = plane(monkeypatch, row=FakeRow(status="done"), bus=FakeBus(pending="call-1"))

    with pytest.raises(Conflict, match="not awaiting a decision"):
        await p.reject(RUN_ID, "call-1", "no")

    assert bus.inbox == []


async def test_a_decision_for_a_different_call_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the guard: the right status but the wrong call id.

    This is the replay the docstring describes — a human authorising something they never
    saw, because the id they approved is not the one the run is parked on.
    """
    p, bus = plane(monkeypatch, bus=FakeBus(pending="the-call-shown-to-the-human"))

    with pytest.raises(Conflict, match="waiting on the-call-shown-to-the-human"):
        await p.approve(RUN_ID, "some-other-call")

    assert bus.inbox == []


async def test_a_decision_for_the_pending_call_is_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterweight: with the guard passing, the decision must actually arrive.

    Without this the three tests above would all pass on a `_pending` that refused
    everything, which would be a gate nobody could get through.
    """
    p, bus = plane(monkeypatch, bus=FakeBus(pending="call-1"))
    await p.approve(RUN_ID, "call-1")
    assert bus.inbox == [{"type": "approve", "tool_call_id": "call-1"}]

    p, bus = plane(monkeypatch, bus=FakeBus(pending="call-2"))
    await p.reject(RUN_ID, "call-2", "not on this repository")
    assert bus.inbox == [
        {"type": "reject", "tool_call_id": "call-2", "reason": "not on this repository"}
    ]


async def test_a_run_waiting_on_nothing_says_so_rather_than_naming_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`awaiting_input` with no pending id is a real state — a question, not a tool call.
    The message has to distinguish it, or the caller retries with the same id forever."""
    p, _ = plane(monkeypatch, bus=FakeBus(pending=None))
    with pytest.raises(Conflict, match="waiting on nothing"):
        await p.approve(RUN_ID, "call-1")


# ---- an answer has to be matchable to its question ---------------------------------------


async def test_an_answers_tool_call_id_reaches_the_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The id is how the worker matches an answer to the question it answers.

    Replaced with `None` — or with `""` — the message still arrives, the run still resumes,
    and the answer is attached to nothing. Both mutations survived the suite.
    """
    p, bus = plane(monkeypatch)
    await p.answer(RUN_ID, "the answer text", "call-abc")

    assert bus.inbox == [{"type": "answer", "text": "the answer text", "tool_call_id": "call-abc"}]


async def test_an_answer_with_no_call_id_omits_the_key_rather_than_nulling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A free-form question has no tool call behind it, and `tool_call_id: None` is not the
    same message as no `tool_call_id` — the worker would have to decide what a null means."""
    p, bus = plane(monkeypatch)
    await p.answer(RUN_ID, "the answer text")

    assert bus.inbox == [{"type": "answer", "text": "the answer text"}]
    assert "tool_call_id" not in bus.inbox[0]


async def test_an_answer_is_refused_when_the_run_is_not_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, bus = plane(monkeypatch, row=FakeRow(status="running"))
    with pytest.raises(Conflict, match="not awaiting input"):
        await p.answer(RUN_ID, "unsolicited")
    assert bus.inbox == []


# ---- the clamp ----------------------------------------------------------------------------


async def test_the_row_limit_is_clamped_at_both_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clamped here rather than at each transport, which is the stated reason: "a caller
    that asks for a million rows is not owed them", over MCP as over HTTP.

    Dropping the upper bound survived the suite because the only assertion anywhere was a
    200 status code, and an unbounded query returns 200 perfectly well right up until the
    table is large.
    """
    for asked, expected in ((1_000_000, MAX_ROWS), (MAX_ROWS + 1, MAX_ROWS), (0, 1), (-5, 1)):
        seen: dict[str, Any] = {}
        p, _ = plane(monkeypatch, created=seen)
        await p.list_runs(asked)
        assert seen["limit"] == expected, f"asked {asked}, expected {expected}"


async def test_a_reasonable_limit_passes_through_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So the clamp cannot become "always return MAX_ROWS", which would pass every
    assertion above while ignoring the caller entirely."""
    seen: dict[str, Any] = {}
    p, _ = plane(monkeypatch, created=seen)
    await p.list_runs(7)
    assert seen["limit"] == 7


# ---- the queue being down is not the caller's problem -------------------------------------


async def test_a_run_is_still_created_when_the_queue_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented degraded path: "the run stays queued rather than silently vanishing,
    and something that reads the table can still pick it up".

    Turning it into an error survived the suite, and it is the difference between an outage
    that delays runs and an outage that rejects them.
    """
    p, _ = plane(monkeypatch, arq=None)
    run_id = await p.create_run(
        repo_url="https://github.com/acme/demo",
        goal="A goal long enough to pass the contract's floor.",
        base_branch="main",
        provider="openai_compat",
        budget=Budget(),
    )
    assert run_id == RUN_ID, "the row was written even with no queue to enqueue onto"


async def test_a_run_is_enqueued_under_its_own_id_when_the_queue_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side, so the test above cannot pass on a plane that never enqueues at all.

    The job id is the run id on purpose — arq deduplicates on it, so a retried enqueue does
    not start the same run twice.
    """
    arq = FakeArq()
    p, _ = plane(monkeypatch, arq=arq)
    await p.create_run(
        repo_url="https://github.com/acme/demo",
        goal="A goal long enough to pass the contract's floor.",
        base_branch="main",
        provider="openai_compat",
    )
    assert arq.jobs == [("run_job", (str(RUN_ID),))]


# ---- a missing run is a NotFound, not an AttributeError -----------------------------------


async def test_an_unknown_run_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every method above calls `get_run` first, so this is the error every one of them
    depends on raising rather than returning `None` for something to trip over later."""
    p, _ = plane(monkeypatch, row=None)
    with pytest.raises(NotFound):
        await p.get_run(RUN_ID)
