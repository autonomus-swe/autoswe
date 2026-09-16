"""Budgets enforced from inside a step, not only between them.

The transition table already refuses to start a phase that cannot be afforded. These are
about the gap it cannot see: one Coder loop can spend twice a limit before a phase
boundary arrives, so the tool loop is told too.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from contracts import Budget, ToolResult, Usage
from core.errors import BudgetExhausted
from orchestrator.budgets import GRACE_DENIALS, RECONCILE_EVERY_TURNS, BudgetGate
from orchestrator.hooks import OrchestratorHooks

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the two writes these touch; the ledger read is set per-test."""
    import orchestrator.budgets as budgets

    class NullSession:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def noop(*a: Any, **k: Any) -> Any:
        return None

    monkeypatch.setattr(budgets, "session", lambda _engine: NullSession())
    monkeypatch.setattr("orchestrator.hooks.session", lambda _engine: NullSession())
    monkeypatch.setattr("gateway.budget.record_llm_call", noop)
    monkeypatch.setattr("storage.repo.insert_tool_call", noop)
    monkeypatch.setattr("orchestrator.events.emit", noop)


def ledger(monkeypatch: pytest.MonkeyPatch, usage: Usage) -> None:
    """What the llm_calls table would say this run has spent."""

    async def run_cost(*a: Any, **k: Any) -> Usage:
        return usage

    monkeypatch.setattr("orchestrator.budgets.db.run_cost", run_cost)


def gate(*, elapsed: float = 0.0, budget: Budget | None = None, **kw: Any) -> BudgetGate:
    return BudgetGate(
        run_id=uuid4(),
        engine=None,
        budget=budget or Budget(max_usd=1.0, wall_clock_s=600),
        elapsed=lambda: elapsed,
        warned=kw.pop("warned", set()),
        **kw,
    )


# ---- reading the number from the right place ---------------------------------------


async def test_the_number_comes_from_the_ledger_not_the_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-memory counters forget what a crashed step spent; the table does not."""
    ledger(monkeypatch, Usage(cost_usd=0.75))
    g = gate(usage=Usage(cost_usd=0.01))  # a stale cache
    assert await g.reconcile() == Usage(cost_usd=0.75)
    assert g.usage.cost_usd == 0.75


async def test_a_run_inside_its_budget_is_not_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger(monkeypatch, Usage(cost_usd=0.10))
    g = gate()
    await g.reconcile()
    assert g.exhausted() is None


async def test_spending_the_dollars_names_the_dollars(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger(monkeypatch, Usage(cost_usd=1.5))
    g = gate()
    await g.reconcile()
    assert g.exhausted() == "budget_usd"


async def test_wall_clock_runs_out_without_anyone_spending_anything() -> None:
    """A step doing nothing but running tools would otherwise never notice the clock."""
    g = gate(elapsed=601.0)
    assert g.exhausted() == "budget_wall_clock", "checked live, not at the last reconciliation"


async def test_an_unpriced_model_is_still_bounded_by_the_clock() -> None:
    g = gate(elapsed=601.0, cost_measurable=False)
    assert g.exhausted() == "budget_wall_clock"


async def test_the_ledger_is_re_read_every_tenth_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = 0

    async def run_cost(*a: Any, **k: Any) -> Usage:
        nonlocal reads
        reads += 1
        return Usage(cost_usd=0.1)

    monkeypatch.setattr("orchestrator.budgets.db.run_cost", run_cost)
    g = gate()
    for _ in range(RECONCILE_EVERY_TURNS * 2):
        await g.on_turn()
    assert reads == 2, "a query per model call for a slow-moving number would be waste"


# ---- warnings ----------------------------------------------------------------------


async def test_a_warning_fires_once_per_run_not_once_per_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger(monkeypatch, Usage(cost_usd=0.95))
    warnings: list[tuple[str, float]] = []
    warned: set[str] = set()  # the run's set, shared by every step's gate

    async def on_warning(kind: str, fraction: float) -> None:
        warnings.append((kind, fraction))

    for _ in range(3):  # three steps in one run
        await gate(warned=warned, on_warning=on_warning).reconcile()

    assert [k for k, _ in warnings] == ["budget_usd"]
    assert warnings[0][1] == pytest.approx(0.95)


async def test_a_spent_budget_is_not_warned_about_it_is_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger(monkeypatch, Usage(cost_usd=2.0))
    warnings: list[str] = []

    async def on_warning(kind: str, fraction: float) -> None:
        warnings.append(kind)

    g = gate(on_warning=on_warning)
    await g.reconcile()
    assert warnings == [], "past 100% there is nothing to warn about"
    assert g.exhausted() == "budget_usd"


# ---- what the tool loop is told -----------------------------------------------------


def hooks(g: BudgetGate | None, **kw: Any) -> OrchestratorHooks:
    return OrchestratorHooks(
        run_id=uuid4(),
        step_id=uuid4(),
        engine=None,
        bus=None,  # the cancel check is not what these test
        provider_name="test",
        model="test/model",
        effort=None,
        role=kw.pop("role", "coder"),
        budget=g,
        **kw,
    )


async def test_an_exhausted_run_may_still_land_its_work() -> None:
    """The denial is not a punishment: the point is to commit what is consistent."""
    h = hooks(gate(elapsed=601.0))
    assert await h.before_tool("git_commit", {"message": "wip"}) is None
    assert await h.before_tool("submit_result", {}) is None


async def test_an_exhausted_run_is_refused_new_work() -> None:
    h = hooks(gate(elapsed=601.0))
    denial = await h.before_tool("str_replace_based_edit_tool", {"command": "create"})
    assert denial is not None
    assert "out of budget" in denial and "budget_wall_clock" in denial
    assert "submit_result" in denial, "a refusal without a way out is just a wall"


async def test_reading_is_still_allowed_when_the_budget_is_gone() -> None:
    """Deciding what to commit may need a look at what is there."""
    h = hooks(gate(elapsed=601.0))
    assert await h.before_tool("read_file", {"path": "a.py"}) is None


async def test_a_step_that_will_not_land_is_stopped_rather_than_refused_forever() -> None:
    """Every denial costs a model turn, so the refusals are counted."""
    h = hooks(gate(elapsed=601.0))
    for _ in range(GRACE_DENIALS):
        assert await h.before_tool("str_replace_based_edit_tool", {}) is not None
    with pytest.raises(BudgetExhausted, match="budget_wall_clock"):
        await h.before_tool("str_replace_based_edit_tool", {})


async def test_an_unaffordable_run_does_not_spend_a_humans_attention() -> None:
    """The budget check is before the approval checks, and that ordering is the point.

    `ask_user` is not mutating, so a budget gate that only looked at `mutating` would let
    it through and park a run that cannot afford to act on the answer on somebody's inbox.
    Attention is the one budget with no dollar figure.
    """

    class Approvals:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def wait(self, kind: str, name: str, call_id: str, input: Any) -> Any:
            self.asked.append(name)
            raise AssertionError("an unaffordable run must not ask")

    approvals = Approvals()
    h = hooks(gate(elapsed=601.0), approvals=approvals)
    denial = await h.before_tool("ask_user", {"question": "which scheme?"})

    assert denial is not None and "out of budget" in denial
    assert approvals.asked == [], "nobody was disturbed"


async def test_a_question_is_still_allowed_while_there_is_budget() -> None:
    """The guard above must not become a blanket refusal."""

    class Approvals:
        async def wait(self, kind: str, name: str, call_id: str, input: Any) -> Any:
            from orchestrator.approvals import Decision

            return Decision(approved=True, answer="JWT")

    h = hooks(gate(elapsed=1.0), approvals=Approvals(), answers={})
    assert await h.before_tool("ask_user", {"question": "which scheme?"}) is None


async def test_a_run_with_room_left_is_not_gated_at_all() -> None:
    h = hooks(gate(elapsed=1.0))
    assert await h.before_tool("str_replace_based_edit_tool", {}) is None


async def test_a_step_with_no_gate_behaves_as_before() -> None:
    h = hooks(None)
    assert await h.before_tool("str_replace_based_edit_tool", {}) is None


async def test_the_hypothesis_gate_still_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Debugger out of budget is told about the hypothesis, not the budget: it cannot
    commit a fix it has not diagnosed, and one message at a time is what gets read."""
    h = hooks(gate(elapsed=601.0), role="debugger")
    denial = await h.before_tool("str_replace_based_edit_tool", {})
    assert denial is not None and "submit_hypothesis" in denial


# ---- the model call that would otherwise be invisible --------------------------------


async def test_a_structured_call_reaches_the_ledger_through_the_hooks() -> None:
    """Budgets are enforced from llm_calls, so a call that skips the hooks is spend the
    run cannot see — worse than spend it cannot afford."""
    from agents.tester import TesterAgent
    from contracts import Triage

    recorded: list[Usage] = []

    class Recorder:
        async def before_tool(self, name: str, input: dict[str, Any]) -> str | None:
            return None

        async def after_tool(
            self, name: str, input: dict[str, Any], result: ToolResult, duration_ms: int
        ) -> ToolResult:
            return result

        async def on_message(self, message: Any, usage: Usage, latency_ms: int) -> None:
            recorded.append(usage)

    class Provider:
        provider_name = "test"
        model = "test/model"

        async def parse(self, req: Any, output: Any) -> Any:
            return Triage(classifications=[]), Usage(input_tokens=40, cost_usd=0.002)

        async def run_tools(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("triage does not use tools")

    from tools import test_report as tr

    await TesterAgent().classify_unknown(
        Provider(),
        [tr.failure("tests/a.py::test_one", "", "exception")],
        Recorder(),
    )
    assert [u.cost_usd for u in recorded] == [0.002]
