"""Every column that carries a caller's request is read by the run it belongs to.

Four times in Phase 6 a `runs` column was written by the API, read back by the API, shown
in the console — and consulted by nothing:

| column | shipped decorative in | noticed in |
|---|---|---|
| `unattended` | Phase 1 | 6.1 |
| `provider` | Phase 1 | 6.3 |
| `budget` | Phase 1 | after 6.6, by an ablation |
| `Budget.max_debug_attempts` | Phase 3 | the base-commit work |

Every one looked correct from both ends. The API stored what it was given and returned it
unchanged; the orchestrator built a state that was internally consistent. Only the join
was missing, and nothing in between had a reason to look.

So this tests the join, in two halves that fail differently:

**Classification** — every column is either a request or an output. Adding one forces a
choice, which is the point: the failure mode is not a wrong decision but an absent one.

**Delivery** — a row written with a distinctive value for every request column produces a
run that can see each of them. This is the test the four bugs above would each have
failed, and it is written so the fifth one fails it too.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from contracts import Budget
from orchestrator.resume import initial_state
from orchestrator.state import RunState
from orchestrator.worker import run_provider
from storage import repo as db
from storage.db import session
from storage.models import RunRow

pytestmark = pytest.mark.integration

# What the caller asked for. Each must be visible to the run, and `DELIVERED` below says
# where to look.
REQUEST_COLUMNS = frozenset(
    {
        "repo_url",
        "base_branch",
        "goal",
        "budget",
        "provider",
        "unattended",
        "base_commit",
        "upstream",
    }
)

# What the run produced, or bookkeeping. Reading one of these *as* a request would be the
# opposite mistake — `base_sha` in particular is what SETUP resolved, not what was asked
# for, and conflating the two is how "the branch moved under us" stops being visible.
OUTPUT_COLUMNS = frozenset(
    {
        "id",
        "work_branch",
        "base_sha",
        "phase",
        "status",
        "cost_usd",
        "pr_url",
        "error",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    }
)

# Column -> how the run sees it. Written out rather than derived, because a derivation
# would follow the same code the test exists to distrust.
DELIVERED: dict[str, Callable[[RunState], Any]] = {
    "repo_url": lambda s: s.repo_url,
    "base_branch": lambda s: s.base_branch,
    "goal": lambda s: s.goal,
    "budget": lambda s: s.budget,
    "unattended": lambda s: s.unattended,
    "base_commit": lambda s: s.base_commit,
    "upstream": lambda s: s.upstream,
    # `provider` is the exception and not an omission: the worker reads it from the row
    # before it builds `Deps`, because the provider has to exist before a state can be
    # loaded. `test_the_provider_is_read_from_the_row` below covers it.
}

ASKED: dict[str, Any] = {
    "repo_url": "https://github.com/acme/asked-for",
    "base_branch": "not-main",
    "goal": "A goal distinctive enough that a default could not be mistaken for it.",
    "budget": Budget(max_usd=3.5, max_debug_attempts=0, max_fix_rounds=1, wall_clock_s=601),
    "provider": "openai_compat",
    "unattended": True,
    "base_commit": "a" * 40,
    "upstream": "them/project",
}


def test_every_column_is_either_a_request_or_an_output() -> None:
    """Adding a column forces a decision about which it is.

    The four bugs were not wrong decisions; they were absent ones. Nobody chose to ignore
    `budget` — it was added to the table and to the API, and the question of whether
    anything read it was never asked.
    """
    columns = {c.name for c in RunRow.__table__.columns}
    unclassified = columns - REQUEST_COLUMNS - OUTPUT_COLUMNS
    assert unclassified == set(), (
        f"new columns {sorted(unclassified)}: add each to REQUEST_COLUMNS (and to "
        "DELIVERED, with a test that the run can see it) or to OUTPUT_COLUMNS"
    )
    assert not (REQUEST_COLUMNS & OUTPUT_COLUMNS), "a column cannot be both"


def test_every_request_column_has_somewhere_to_be_looked_for() -> None:
    """`DELIVERED` covers the request columns, minus the one with its own test."""
    assert set(DELIVERED) | {"provider"} == REQUEST_COLUMNS


async def make_row(engine: AsyncEngine) -> uuid.UUID:
    async with session(engine) as s:
        return await db.create_run(
            s,
            repo_url=str(ASKED["repo_url"]),
            base_branch=str(ASKED["base_branch"]),
            goal=str(ASKED["goal"]),
            budget=ASKED["budget"],
            provider=str(ASKED["provider"]),
            unattended=bool(ASKED["unattended"]),
            upstream=str(ASKED["upstream"]),
            base_commit=str(ASKED["base_commit"]),
        )


@pytest.mark.parametrize("column", sorted(DELIVERED))
async def test_a_request_column_reaches_the_run(engine: AsyncEngine, column: str) -> None:
    """Parametrised so a failure names the field rather than the row.

    "the run did not get what it was asked for" is true of four separate shipped bugs;
    "`budget` did not reach the run" is the one that gets fixed.
    """
    state = await initial_state(engine, await make_row(engine))
    assert DELIVERED[column](state) == ASKED[column]


async def test_the_provider_is_read_from_the_row(engine: AsyncEngine) -> None:
    """Its own test because it is read on a different path.

    `Deps` needs a provider before a state can be loaded, so the worker reads the column
    directly rather than through `RunState` — which is why it is absent from `DELIVERED`
    and present here rather than simply forgotten.
    """
    assert await run_provider(engine, await make_row(engine)) == ASKED["provider"]


async def test_a_row_of_defaults_produces_a_state_of_defaults(engine: AsyncEngine) -> None:
    """The other direction, so the test above cannot pass by accident.

    If `initial_state` hard-coded the values this file asks for, every assertion above
    would hold and nothing would work. A row that asked for nothing must come back with
    nothing.
    """
    async with session(engine) as s:
        run_id = await db.create_run(
            s,
            repo_url="https://github.com/acme/plain",
            base_branch="main",
            goal="A plain run that asked for none of the optional things.",
            budget=Budget(),
        )
    state = await initial_state(engine, run_id)
    assert state.budget == Budget()
    assert state.unattended is False
    assert state.base_commit is None
    assert state.upstream is None
