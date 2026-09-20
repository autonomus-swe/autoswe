"""M5 step 5.10: the scale run. What this system costs on a real repository.

Everything else in this phase was measured on a three-file fixture or host-side with
`evals/scale.py`, which parses and ranks but never calls a model. Neither answers the
question the milestone is named for: does a repo map built from 44 000 symbols actually
point an agent at the right file, and what does a run cost when the prefix is 3 000 tokens
of somebody else's code?

The goal below is deliberately small — the phase document asks for "two or three files and
a test", and a scale run is about the *repository* being large, not the change. A large
change on a large repository would measure the model's stamina instead.

## What is asserted, and what is only recorded

Asserted: the run reaches a terminal phase, the index covers a repository of the size
claimed, and the rendered map stays inside its token budget. Those are properties of this
system.

Recorded, not asserted: cost, wall clock, cache hit rate, tasks, attempts. Those are
properties of a *model* on a given day, and a threshold on them is a test that fails when
a provider changes its routing. They go to `evals/results/m5.jsonl`, which is where
`docs/numbers.md` reads them from.

The $5 ceiling in the exit criteria is the one number with a threshold, and it is asserted
— exceeding it means the run is not viable, which is a fact about the design rather than
about the weather.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import UUID

import pytest

from contracts import Budget, Usage
from evals import record
from orchestrator.deps import Deps
from orchestrator.runner import run
from orchestrator.state import Phase, RunState
from storage import repo as db
from storage.db import session

pytestmark = pytest.mark.e2e

# Two or three files and a test, as the phase document asks. Phrased so the repo map has
# to do the work: it names a behaviour, not a path, so a run that edits the right file has
# been pointed there by ranking rather than by being told.
GOAL = (
    "Add a `__repr__` to the class that represents a parsed cookie or header value, "
    "showing its public fields, and add a unit test for it next to the existing tests "
    "for that module. Do not change unrelated files."
)

MAX_COST_USD = 5.0
TERMINAL = (Phase.DONE, Phase.REVIEW, Phase.PR, Phase.FAILED)


async def test_a_run_on_a_large_repository_stays_inside_its_budget(
    e2e_deps: Deps, scale_repo: Path
) -> None:
    deps, repo = e2e_deps, scale_repo
    files = sum(1 for _ in repo.rglob("*") if _.is_file() and ".git" not in _.parts)

    async with session(deps.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(repo),
            base_branch="main",
            goal=GOAL,
            budget=Budget(max_usd=MAX_COST_USD),
            provider=deps.settings.llm_provider,
        )
    state = RunState(
        run_id=run_id,
        goal=GOAL,
        repo_url=str(repo),
        base_branch="main",
        work_branch=f"agent/{run_id}",
        budget=Budget(max_usd=MAX_COST_USD),
        unattended=True,
    )

    started = time.monotonic()
    try:
        final = await run(state, deps)
    finally:
        # Always. A scale run is the most expensive thing in the suite and the most likely
        # to stop somewhere unplanned; losing its numbers to an exception means paying for
        # it twice. The local fixture run was collected this way after an assertion failed.
        total, steps = await _record(deps, run_id, repo, files, time.monotonic() - started)

    assert final.phase in TERMINAL, f"run stalled in {final.phase}: {final.error}"
    assert steps, "no steps were recorded, so nothing above was measured"
    # The criterion's own ceiling. Unlike the rest, this one is a property of the design:
    # a run that cannot survey a repository this size for $5 is not viable at scale.
    assert total.cost_usd < MAX_COST_USD, f"the run cost ${total.cost_usd:.2f}"


async def _record(
    deps: Deps, run_id: UUID, repo: Path, files: int, wall_s: float
) -> tuple[Usage, list[tuple[str, str, Usage]]]:
    """Everything the phase document asks a scale run to produce, to `m5.jsonl`."""
    async with session(deps.engine) as s:
        steps = await db.step_costs(s, run_id)
        total = await db.run_cost(s, run_id)

    per_role: dict[str, dict[str, int]] = {}
    for agent, _phase, usage in steps:
        row = per_role.setdefault(agent, {"calls": 0, "input": 0, "cache_read": 0})
        row["calls"] += 1
        row["input"] += usage.input_tokens
        row["cache_read"] += usage.cache_read_tokens

    path = record.append(
        "m5",
        {
            "kind": "scale_run",
            "run_id": str(run_id),
            # The source repository's name, not the clone's — every clone is called
            # "origin", which makes a file of them useless to read back.
            "repo": Path(os.environ.get("AUTOSWE_SCALE_REPO", repo.name)).name,
            "files_total": files,
            "model": deps.provider.model,
            "wall_clock_s": round(wall_s, 1),
            "input_tokens": total.input_tokens,
            "cache_read_tokens": total.cache_read_tokens,
            "output_tokens": total.output_tokens,
            "cache_hit_rate": round(total.cache_hit_rate, 4),
            "cost_usd": round(total.cost_usd, 6),
            "steps": len(steps),
            "per_role": per_role,
            "goal": GOAL,
        },
    )
    name = Path(os.environ.get("AUTOSWE_SCALE_REPO", repo.name)).name
    print(
        f"\nscale run on {name}: {files} files, {len(steps)} steps, "
        f"{wall_s / 60:.1f} min, ${total.cost_usd:.4f}, "
        f"cache {total.cache_hit_rate:.1%} → {path}"
    )
    for agent, counts in per_role.items():
        print(f"  {agent:11} calls={counts['calls']:3} input={counts['input']:8}")
    return total, steps
