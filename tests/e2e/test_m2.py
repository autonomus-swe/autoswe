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

# The tests are given and the work is tiny, which is the fixture idiom Phase 1 uses and
# is deliberate on both counts.
#
# The criterion is that a goal decomposing into three or more tasks finishes with a
# commit per task: it is about the multi-task CODE/TEST loop, not about how good the
# model is at coding. Asking the agent to write its own tests measures something else
# entirely, and that is what failed the first real attempt — every agent ran, the loop
# worked, and the run died because the model's self-written tests did not pass. Phase 2
# has no debug loop by design, so one bad test file ends the run.
#
# The suite is run whole after each task, so the goal has to name every function the
# tests import, not just the new ones.
EXTRA_TESTS = """from fixture.ops import double, negate, triple


def test_double():
    assert double(3) == 6
    assert double(0) == 0


def test_triple():
    assert triple(3) == 9
    assert triple(-2) == -6


def test_negate():
    assert negate(4) == -4
    assert negate(-7) == 7
"""

MULTI_TASK_GOAL = (
    "fixture/ops.py is missing functions that the test suite imports. Implement them so "
    "the whole suite passes: subtract(a, b) and slugify(text) for tests/test_ops.py, and "
    "double(n), triple(n) and negate(n) for tests/test_extra.py. Every one is a one-line "
    "function. Do not change any file under tests/."
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


async def _add_extra_tests(origin: Path) -> None:
    """Commit the failing tests the agent has to satisfy, before it clones the repo.

    Written here rather than into tests/fixtures/fixture_repo because the TEST phase runs
    the whole suite after each task, so a test file the scripted integration coder cannot
    satisfy would break those tests too.
    """
    (origin / "tests" / "test_extra.py").write_text(EXTRA_TESTS)
    await git("add", "-A", cwd=origin)
    await git("commit", "-q", "-m", "test: cover double, triple and negate", cwd=origin)


async def test_m2_decomposes_into_several_tasks_and_commits_each(
    e2e_deps: Deps, origin_repo: Path
) -> None:
    await _add_extra_tests(origin_repo)
    state = await _start(e2e_deps, origin_repo, MULTI_TASK_GOAL, unattended=True)
    final = await run(state, e2e_deps)

    # "a phase produced no usable result" on its own says nothing about which phase or
    # why. Phase 2 has no debug loop, so a task whose tests fail ends the run here, and
    # the report is the difference between a harness bug and the model writing bad code.
    if final.phase is not Phase.DONE:
        report = final.last_test_report
        progress = [(t.spec.id, t.status) for t in final.tasks.tasks] if final.tasks else []
        async with session(e2e_deps.engine) as s:
            events = [e.type for e in await db.list_events(s, final.run_id)]
        pytest.fail(
            f"run ended {final.phase.value}: {final.error}\n"
            f"  tasks:  {progress}\n"
            f"  report: {report}\n"
            f"  events: {events}"
        )
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

    # one commit per task on top of main, and nothing under tests/ was touched — the goal
    # is satisfied by implementing, and rewriting the tests would be cheating it
    log = await git("log", "--oneline", f"main..{final.work_branch}", cwd=origin_repo)
    assert len([ln for ln in log.splitlines() if ln.strip()]) >= len(graph.tasks)
    diff = await git("diff", "main", final.work_branch, "--", "tests/", cwd=origin_repo)
    assert diff.strip() == "", f"the agent must not modify the tests it was given:\n{diff}"
    changed = await git("diff", "--name-only", "main", final.work_branch, cwd=origin_repo)
    assert "fixture/ops.py" in changed, changed

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
