"""The budget a run was asked for is the budget it gets.

`runs.budget` had been written by the API since Phase 1 and never read: `initial_state`
built a fresh `Budget()` and the column was decoration. A caller asking for a $3 ceiling
got $10, and every `--budget`, every `budget_usd` over MCP and every eval task's ceiling
was recorded and ignored.

It was found by running an ablation arm — `--ablate no-debugger` sets
`max_debug_attempts=0`, and a Debugger step ran anyway. The row said 0; the state said 3.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from contracts import Budget
from orchestrator.resume import budget_from

pytestmark = pytest.mark.unit


def row(budget: Any) -> Any:
    return SimpleNamespace(id="11111111-2222-3333-4444-555555555555", budget=budget)


def test_the_ceiling_the_caller_asked_for_is_the_one_that_is_used() -> None:
    got = budget_from(row({"max_usd": 3.0}))
    assert got.max_usd == 3.0, "a $3 run must not get a $10 ceiling"


def test_every_field_survives_the_round_trip() -> None:
    """Not just the dollars. `max_debug_attempts` is what the `no-debugger` ablation sets,
    and `wall_clock_s` is what stops a run that will never finish."""
    asked = Budget(max_usd=3.0, max_debug_attempts=0, max_fix_rounds=1, wall_clock_s=600)
    got = budget_from(row(asked.model_dump(mode="json")))
    assert got == asked


def test_fields_the_caller_left_out_take_their_defaults() -> None:
    """A partial budget is the normal case — `{"max_usd": 3}` is what the CLI sends."""
    got = budget_from(row({"max_usd": 3.0}))
    assert got.max_debug_attempts == Budget().max_debug_attempts
    assert got.wall_clock_s == Budget().wall_clock_s


@pytest.mark.parametrize("stored", [None, {}])
def test_a_row_with_no_budget_gets_the_defaults(stored: Any) -> None:
    assert budget_from(row(stored)) == Budget()


def test_a_row_that_no_longer_validates_falls_back_rather_than_refusing() -> None:
    """`Budget` forbids extras, so a field removed since the row was written would raise.

    A resumed run dying over a budget key is worse than one running on the default
    ceiling — but silence is what let the original bug sit for five phases, so it is
    logged at error rather than swallowed.
    """
    got = budget_from(row({"max_usd": 3.0, "a_field_that_was_removed": 1}))
    assert got == Budget(), "the whole budget falls back, not just the bad key"


def test_a_zero_debug_ceiling_survives_and_is_not_read_as_absent() -> None:
    """The ablation arm's value is falsy, and a `or`-style default would erase it —
    which is the shape of bug that hides inside a fix for this one."""
    assert budget_from(row({"max_debug_attempts": 0})).max_debug_attempts == 0
