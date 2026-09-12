"""M2 end to end with a real model: a goal that decomposes into several tasks, and a
goal vague enough that the planner should stop and ask before writing any code."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from contracts import Budget
from orchestrator.deps import Deps
from orchestrator.runner import run
from orchestrator.state import Phase, RunState
from repo.gitcmd import git
from storage import repo as db
from storage.db import session

pytestmark = pytest.mark.e2e

MULTI_TASK_GOAL = (
    "Add a Stats class in fixture/stats.py with mean(), median() and mode(); a CLI entry "
    "point fixture/__main__.py that reads numbers from stdin and prints all three; and "
    "tests for each in tests/test_stats.py. Do not change the existing tests."
)
AMBIGUOUS_GOAL = "Add authentication."
ANSWER = "JWT with HS256, secret from env JWT_SECRET, access tokens only, no refresh tokens."


async def _start(deps: Deps, origin: Path, goal: str, *, unattended: bool) -> RunState:
    async with session(deps.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(origin),
            base_branch="main",
            goal=goal,
            budget=Budget(max_usd=4.0),
            provider=deps.settings.llm_provider,
            unattended=unattended,
        )
    return RunState(
        run_id=run_id,
        goal=goal,
        repo_url=str(origin),
        base_branch="main",
        work_branch=f"agent/{run_id}",
        unattended=unattended,
    )


async def _wait_for_status(deps: Deps, run_id: object, status: str, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with session(deps.engine) as s:
            row = await db.get_run(s, run_id)  # type: ignore[arg-type]
        if row is not None and row.status == status:
            return
        await asyncio.sleep(2.0)
    raise AssertionError(f"run never reached {status} within {timeout_s}s")


async def test_m2_decomposes_into_several_tasks_and_commits_each(
    e2e_deps: Deps, origin_repo: Path
) -> None:
    state = await _start(e2e_deps, origin_repo, MULTI_TASK_GOAL, unattended=True)
    final = await run(state, e2e_deps)

    assert final.phase is Phase.DONE, f"run failed: {final.error}"
    graph = final.tasks
    assert graph is not None and len(graph.tasks) >= 3, graph
    assert all(t.status == "done" for t in graph.tasks), [
        (t.spec.id, t.status) for t in graph.tasks
    ]

    async with session(e2e_deps.engine) as s:
        tasks = await db.list_tasks(s, final.run_id)
        steps = await db.list_steps(s, final.run_id)
    assert len(tasks) >= 3
    # the plan and the decomposition each happened exactly once
    assert [st.agent for st in steps].count("planner") == 1
    assert [st.agent for st in steps].count("decomposer") == 1

    # one commit per task on top of main, and the existing tests are untouched
    log = await git("log", "--oneline", f"main..{final.work_branch}", cwd=origin_repo)
    assert len([ln for ln in log.splitlines() if ln.strip()]) >= len(graph.tasks)
    diff = await git("diff", "main", final.work_branch, "--", "tests/test_ops.py", cwd=origin_repo)
    assert diff.strip() == "", "the agent must not modify the tests it was given"

    report = final.last_test_report
    assert report is not None and report.passed


async def test_m2_pauses_on_an_ambiguous_goal_and_resumes_after_an_answer(
    e2e_deps: Deps, origin_repo: Path
) -> None:
    """`unattended=False` means the planner is allowed to stop and ask."""
    state = await _start(e2e_deps, origin_repo, AMBIGUOUS_GOAL, unattended=False)
    task = asyncio.create_task(run(state, e2e_deps))
    try:
        await _wait_for_status(e2e_deps, state.run_id, "awaiting_input", timeout_s=600)

        async with session(e2e_deps.engine) as s:
            rows = await db.list_events(s, state.run_id)
        asked = [e for e in rows if e.type == "awaiting_input"]
        assert asked and asked[-1].payload.get("questions"), "the pause must say what it needs"

        await e2e_deps.bus.push_inbox(state.run_id, {"type": "answer", "text": ANSWER})
        final = await asyncio.wait_for(task, timeout=1800)
    finally:
        if not task.done():
            task.cancel()

    assert final.phase in (Phase.DONE, Phase.FAILED), final.phase
    assert final.answers and ANSWER in final.answers[-1][1]
    assert final.waiting_s > 0, "time spent waiting on a human is not agent time"

    # the audit trail shows which plan was driven by the human's answer
    async with session(e2e_deps.engine) as s:
        steps = await db.list_steps(s, state.run_id)
    planner = [st for st in steps if st.agent == "planner"]
    assert planner, [st.agent for st in steps]
    replanned = [st for st in planner if (st.input or {}).get("answers")]
    assert replanned, [st.input for st in planner]
    assert ANSWER in str(replanned[-1].input)
