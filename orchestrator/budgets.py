"""Enforcing a budget from inside a step, not just between them.

``transition`` already refuses to start another phase once a budget is gone. That is not
enough on its own: a Coder loop can run for dozens of turns, and a run that only checks
its budget at phase boundaries can spend twice the limit inside one step before anything
looks. So the tool loop gets a view of what has been spent.

Two rules about *what* it reads:

The number comes from the ``llm_calls`` table, not from the in-memory counter. The counter
is a cache — it forgets everything a crashed step spent, and it misses any model call that
did not go through these hooks. The table is what a bill is reconciled against, so it is
what a budget is enforced from.

Waiting for a human is not spending. ``elapsed`` subtracts ``waiting_s``, so a run parked
on an approval overnight does not wake up over its wall clock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from contracts import Budget, Usage
from observability.logging import get_logger
from storage import repo as db
from storage.db import session

log = get_logger(__name__)

# How often to go back to the database mid-step. Every turn would be a query per model
# call for a number that moves slowly; ten is frequent enough to catch a runaway loop.
RECONCILE_EVERY_TURNS = 10
# Denials allowed after the budget is gone before the step is stopped outright. The model
# is told to commit and submit; a cooperative one needs a turn or two, and a stuck one
# would otherwise spend every remaining iteration being refused.
GRACE_DENIALS = 3

# Allowed even when the budget is gone: the point of the denial is to land the work, and
# these are how it lands. Neither can start anything new.
ALWAYS_ALLOWED = ("git_commit", "git_status", "submit_result", "submit_hypothesis")


@dataclass
class BudgetGate:
    """What the tool loop is allowed to know about spending.

    Deliberately not the run state: a hook that could read the whole state would end up
    steering the run, and the phase machine is the only thing that gets to do that.
    """

    run_id: UUID
    engine: Any
    budget: Budget
    # Reads the run's clock. A callable rather than a number because a step can outlast
    # any value captured when it started.
    elapsed: Callable[[], float]
    # The run's `warned` set, by reference: a warning is emitted once per run, not once
    # per step, or a long run would repeat it at every phase.
    warned: set[str]
    cost_measurable: bool = True
    on_warning: Callable[[str, float], Any] | None = None
    usage: Usage = field(default_factory=Usage)
    turns: int = 0
    denials: int = 0
    _last_reason: str | None = None

    async def reconcile(self) -> Usage:
        """Re-read what this run has actually spent, and warn if a limit is close."""
        async with session(self.engine) as s:
            self.usage = await db.run_cost(s, self.run_id)
        elapsed = self.elapsed()
        for kind in self.budget.crossed_warning(
            self.usage, elapsed, cost_measurable=self.cost_measurable
        ):
            if kind in self.warned:
                continue
            self.warned.add(kind)
            fraction = self.budget.fraction_used(
                self.usage, elapsed, cost_measurable=self.cost_measurable
            )[kind]
            log.warning("budget_warning", kind=kind, fraction=round(fraction, 3))
            if self.on_warning is not None:
                await self.on_warning(kind, fraction)
        self._last_reason = (
            self.budget.reason(self.usage, elapsed, cost_measurable=self.cost_measurable)
            if self.budget.exceeded(self.usage, elapsed, cost_measurable=self.cost_measurable)
            else None
        )
        return self.usage

    async def on_turn(self) -> None:
        """Called once per model turn. Goes back to the database every tenth."""
        self.turns += 1
        if self.turns % RECONCILE_EVERY_TURNS == 0:
            await self.reconcile()

    def exhausted(self) -> str | None:
        """Which budget ran out, as of the last reconciliation. None while there is room.

        Wall clock is checked here rather than at reconciliation time because it passes
        without anyone spending anything: a step that stopped calling the model but is
        still running tools would otherwise never notice.
        """
        if self._last_reason is not None:
            return self._last_reason
        elapsed = self.elapsed()
        if self.budget.exceeded(self.usage, elapsed, cost_measurable=self.cost_measurable):
            return self.budget.reason(self.usage, elapsed, cost_measurable=self.cost_measurable)
        return None

    def denial(self, reason: str, tool_name: str) -> str:
        """The message the model gets, and the count that stops this step eventually."""
        self.denials += 1
        return (
            f"denied: the run is out of budget ({reason}). {tool_name} would start new work. "
            "Commit whatever is already consistent with git_commit, then call submit_result "
            "and say in the summary what is unfinished."
        )

    def spent_its_grace(self) -> bool:
        """True once the allowance is used up: three denials issued, not four."""
        return self.denials >= GRACE_DENIALS
