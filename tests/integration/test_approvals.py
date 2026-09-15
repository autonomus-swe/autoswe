"""Approvals: a run pauses mid-tool-call, a human decides, and the run learns the answer.

Against a real Redis, because the pending-call id and the inbox are how two processes —
the worker that waits and the API that decides — agree on which call is being authorised.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from orchestrator.approvals import (
    TIMEOUT_REJECTION,
    UNATTENDED_REJECTION,
    ApprovalGate,
)
from orchestrator.hooks import OrchestratorHooks
from storage.redis import RedisBus
from tools import policy

pytestmark = pytest.mark.integration


class FakeEngine:
    """The gate writes a run status; these tests are about the decision, not the row."""


def gate(bus: RedisBus, run_id: Any, **kw: Any) -> ApprovalGate:
    return ApprovalGate(run_id=run_id, engine=FakeEngine(), bus=bus, **kw)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two database writes the gate makes, so Redis is the only real dependency."""
    import orchestrator.approvals as approvals

    class NullSession:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def noop(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(approvals, "session", lambda _engine: NullSession())
    monkeypatch.setattr("storage.repo.set_run_status", noop)
    monkeypatch.setattr(approvals, "emit", noop)


async def test_approving_lets_the_call_through(bus: RedisBus) -> None:
    run_id, call_id = uuid4(), "step:1"
    g = gate(bus, run_id)

    async def human() -> None:
        # wait until the run is actually parked on this call before deciding
        for _ in range(100):
            if await bus.get_pending(run_id) == call_id:
                break
            await asyncio.sleep(0.05)
        await bus.push_inbox(run_id, {"type": "approve", "tool_call_id": call_id})

    decision, _ = await asyncio.gather(
        g.wait("command", "bash", call_id, {"command": "uv add requests"}), human()
    )
    assert decision.approved and not decision.reason
    assert await bus.get_pending(run_id) is None, "the pending id is cleared afterwards"


async def test_rejecting_returns_the_reason_to_the_model(bus: RedisBus) -> None:
    run_id, call_id = uuid4(), "step:2"
    g = gate(bus, run_id)

    async def human() -> None:
        for _ in range(100):
            if await bus.get_pending(run_id) == call_id:
                break
            await asyncio.sleep(0.05)
        await bus.push_inbox(
            run_id,
            {"type": "reject", "tool_call_id": call_id, "reason": "vendor it instead"},
        )

    decision, _ = await asyncio.gather(
        g.wait("command", "bash", call_id, {"command": "uv add requests"}), human()
    )
    assert not decision.approved
    assert decision.reason == "vendor it instead"


async def test_an_answer_comes_back_as_the_tool_result(bus: RedisBus) -> None:
    """`ask_user` is approved *and* carries text; the two are not the same thing."""
    run_id, call_id = uuid4(), "step:3"
    g = gate(bus, run_id)

    async def human() -> None:
        for _ in range(100):
            if await bus.get_pending(run_id) == call_id:
                break
            await asyncio.sleep(0.05)
        await bus.push_inbox(run_id, {"type": "answer", "tool_call_id": call_id, "text": "use JWT"})

    decision, _ = await asyncio.gather(
        g.wait("question", "ask_user", call_id, {"question": "which scheme?"}), human()
    )
    assert decision.approved and decision.answer == "use JWT"


async def test_a_decision_for_another_call_is_not_consumed(bus: RedisBus) -> None:
    """Two waiters must not steal each other's answers, so a mismatch is put back."""
    run_id, mine, theirs = uuid4(), "step:4", "step:99"
    g = gate(bus, run_id, timeout_s=3)
    await bus.push_inbox(run_id, {"type": "approve", "tool_call_id": theirs})

    decision = await g.wait("command", "bash", mine, {})
    assert not decision.approved and decision.reason == TIMEOUT_REJECTION
    # the other call's approval is still there for whoever it belongs to
    left = await bus.pop_inbox(run_id, timeout_s=1)
    assert left is not None and left["tool_call_id"] == theirs


async def test_an_answer_without_an_id_is_for_the_phase_not_a_tool(bus: RedisBus) -> None:
    """The Planner's open-question answers must not satisfy a tool approval."""
    run_id = uuid4()
    g = gate(bus, run_id, timeout_s=3)
    await bus.push_inbox(run_id, {"type": "answer", "text": "JWT"})

    decision = await g.wait("question", "ask_user", "step:5", {})
    assert not decision.approved and decision.reason == TIMEOUT_REJECTION


async def test_an_unattended_run_refuses_immediately_without_parking(bus: RedisBus) -> None:
    """Nobody is listening, so hanging would be worse than refusing."""
    run_id = uuid4()
    g = gate(bus, run_id, unattended=True)
    decision = await g.wait("command", "bash", "step:6", {"command": "uv add x"})
    assert not decision.approved and decision.reason == UNATTENDED_REJECTION
    assert await bus.get_pending(run_id) is None, "it never parked"


async def test_cancelling_releases_a_parked_run(bus: RedisBus) -> None:
    run_id, call_id = uuid4(), "step:7"
    g = gate(bus, run_id, timeout_s=60)

    async def canceller() -> None:
        for _ in range(100):
            if await bus.get_pending(run_id) == call_id:
                break
            await asyncio.sleep(0.05)
        await bus.set_cancel(run_id)

    decision, _ = await asyncio.gather(g.wait("command", "bash", call_id, {}), canceller())
    assert not decision.approved and "cancelled" in decision.reason


async def test_waiting_time_is_reported_so_it_leaves_the_budget(bus: RedisBus) -> None:
    waited: list[float] = []
    run_id = uuid4()
    g = gate(bus, run_id, timeout_s=1, on_wait=waited.append)
    await g.wait("command", "bash", "step:8", {})
    assert waited and waited[0] > 0, "a human's thinking time is not the agent's"


async def test_the_lock_is_renewed_while_parked(bus: RedisBus) -> None:
    """A wait can outlast the lock TTL, and losing the repo mid-run would be worse."""
    renewals = 0

    async def renew() -> None:
        nonlocal renewals
        renewals += 1

    g = gate(bus, uuid4(), timeout_s=1, renew=renew)
    await g.wait("command", "bash", "step:9", {})
    assert renewals >= 1


# ---- the hook that calls the gate ---------------------------------------------------


def hooks(bus: RedisBus, run_id: Any, **kw: Any) -> OrchestratorHooks:
    return OrchestratorHooks(
        run_id=run_id,
        step_id=uuid4(),
        engine=FakeEngine(),
        bus=None,  # the cancel check in before_tool is not what these test
        provider_name="test",
        model="test/model",
        effort=None,
        role="coder",
        submitted={},
        **kw,
    )


@pytest.mark.parametrize(
    ("command", "asks"),
    [
        ("uv add requests", True),
        ("pip install foo", True),
        ("rm -rf build", True),
        ("alembic downgrade -1", True),
        ("curl https://example.com", True),
        ("pytest -q", False),
        ("ls -la", False),
        ("rm one_file.txt", False),
    ],
)
async def test_the_ask_list_decides_by_what_a_command_does(
    bus: RedisBus, command: str, asks: bool
) -> None:
    assert (policy.needs_approval(command) is not None) is asks
    h = hooks(bus, uuid4(), approvals=gate(bus, uuid4(), unattended=True), answers={})
    denial = await h.before_tool("bash", {"command": command})
    if asks:
        assert denial is not None and UNATTENDED_REJECTION in denial
    else:
        assert denial is None


async def test_an_approved_question_leaves_its_answer_for_the_tool(bus: RedisBus) -> None:
    """`before_tool` can refuse a call but cannot hand one a value, hence the channel."""
    run_id = uuid4()
    answers: dict[str, str] = {}
    g = gate(bus, run_id)
    h = hooks(bus, run_id, approvals=g, answers=answers)

    async def human() -> None:
        for _ in range(200):
            pending = await bus.get_pending(run_id)
            if pending:
                await bus.push_inbox(
                    run_id, {"type": "answer", "tool_call_id": pending, "text": "session cookies"}
                )
                return
            await asyncio.sleep(0.05)

    denial, _ = await asyncio.gather(
        h.before_tool("ask_user", {"question": "which scheme?"}), human()
    )
    assert denial is None, "an approved call proceeds"
    assert answers == {"which scheme?": "session cookies"}


async def test_a_run_with_no_approver_refuses_rather_than_pretending(bus: RedisBus) -> None:
    h = hooks(bus, uuid4(), approvals=None, answers={})
    denial = await h.before_tool("ask_user", {"question": "anything?"})
    assert denial is not None and "no approver is configured" in denial
