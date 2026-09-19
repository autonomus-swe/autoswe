"""M5: what prompt caching is actually worth, measured on a real run.

Everything in `tests/unit/test_caching.py` checks that the right bytes are assembled and
the right blocks are marked. None of it can tell you whether the provider then *read* the
cache — that number exists only on the other side of a real API call, in
`usage.cache_read_tokens`.

So this is the only place the question is settled, and the pass condition is deliberately
weak: *something* was read from cache, and the Coder in particular read some. A threshold
on the rate would be a threshold on the provider's caching policy, which changes without
telling anyone, and a run failing because a gateway raised its minimum cacheable length
would be a test reporting the wrong thing. The rate itself is recorded rather than
asserted — `m5_cache.jsonl` is where the number lives, and it is a number from a run.

**Not yet run against a real model.** The OpenRouter free tier is 50 requests a day and one
M5 run spends more than that, which is the same wall `m3.jsonl` and `m4.jsonl` are behind —
see `evals/results/README.md`. The test is written against the fixture repository and runs
the moment there is quota; it is not a test that has passed and is being kept, and nothing
in the repository claims a measured caching number until `m5_cache.jsonl` has rows in it.
"""

from __future__ import annotations

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

GOAL = (
    "Add a function `subtract(a, b)` to fixture/ops.py that returns a - b, "
    "and make sure the existing tests still pass."
)


async def test_a_run_reads_its_prefix_from_cache_after_the_first_call(
    e2e_deps: Deps, origin_repo: Path
) -> None:
    deps, repo = e2e_deps, origin_repo
    async with session(deps.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(repo),
            base_branch="main",
            goal=GOAL,
            budget=Budget(max_usd=3.0),
            provider=deps.settings.llm_provider,
        )
    state = RunState(
        run_id=run_id,
        goal=GOAL,
        repo_url=str(repo),
        base_branch="main",
        work_branch=f"agent/{run_id}",
        budget=Budget(max_usd=3.0),
        unattended=True,
    )

    final = await run(state, deps)
    per_step, total = await _cache_report(deps, run_id)

    assert final.phase in (Phase.DONE, Phase.REVIEW, Phase.PR), f"run stalled: {final.error}"
    coder_steps = [(agent, usage) for agent, _phase, usage in per_step if agent == "coder"]
    assert coder_steps, "no Coder step to measure"
    assert total.cache_read_tokens > 0, (
        "nothing was read from cache across the whole run — either the prefix is moving "
        "between calls or this endpoint does not cache at all"
    )
    # The Coder is the role this pays for: one long loop, resent whole on every turn.
    assert any(usage.cache_hit_rate > 0 for _agent, usage in coder_steps)


async def _cache_report(deps: Deps, run_id: UUID) -> tuple[list[tuple[str, str, Usage]], Usage]:
    """Per step and per run, printed and recorded — the numbers are the point of the test."""
    async with session(deps.engine) as s:
        per_step = await db.step_costs(s, run_id)
        total = await db.run_cost(s, run_id)

    path = record.append(
        "m5_cache",
        {
            "run_id": str(run_id),
            "model": deps.provider.model,
            "input_tokens": total.input_tokens,
            "cache_read_tokens": total.cache_read_tokens,
            "cache_write_tokens": total.cache_write_tokens,
            "cache_hit_rate": round(total.cache_hit_rate, 4),
            "cost_usd": round(total.cost_usd, 6),
            "steps": [
                {
                    "agent": agent,
                    "phase": phase,
                    "input_tokens": usage.input_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_hit_rate": round(usage.cache_hit_rate, 4),
                }
                for agent, phase, usage in per_step
            ],
        },
    )
    print(f"\ncache hit rate {total.cache_hit_rate:.1%} over {len(per_step)} steps → {path}")
    for agent, phase, usage in per_step:
        print(f"  {phase:9} {agent:11} {usage.cache_hit_rate:6.1%}  {usage.input_tokens:7} in")
    return per_step, total
