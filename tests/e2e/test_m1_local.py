"""M1 end to end with no GitHub account: clone a local repo, let the Coder work in the
sandbox, run the tests, push the branch, and 'open' a pull request through a stub."""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts import Budget
from orchestrator.deps import Deps
from orchestrator.runner import run
from orchestrator.state import Phase, RunState
from repo.gitcmd import git
from storage import repo as db
from storage.db import session
from tests.e2e.conftest import GOAL, RecordingGitHub

pytestmark = pytest.mark.e2e


async def test_m1_end_to_end(e2e_deps: Deps, origin_repo: Path) -> None:
    async with session(e2e_deps.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(origin_repo),
            base_branch="main",
            goal=GOAL,
            budget=Budget(max_usd=2.0),
            provider=e2e_deps.settings.llm_provider,
        )
    state = RunState(
        run_id=run_id,
        goal=GOAL,
        repo_url=str(origin_repo),
        base_branch="main",
        work_branch=f"agent/{run_id}",
    )

    final = await run(state, e2e_deps)

    assert final.phase is Phase.DONE, f"run failed: {final.error}"
    assert final.task_result is not None and final.pr_url
    report = final.last_test_report
    assert report is not None and report.passed and report.total >= 3

    # the branch really exists in the origin and its tests really pass
    branches = await git("branch", "--list", final.work_branch, cwd=origin_repo)
    assert final.work_branch in branches
    diff = await git("diff", "main", final.work_branch, "--", "tests/", cwd=origin_repo)
    assert diff.strip() == "", "the agent must not modify the tests"
    changed = await git("diff", "--name-only", "main", final.work_branch, cwd=origin_repo)
    assert "fixture/ops.py" in changed

    github = e2e_deps.github
    assert isinstance(github, RecordingGitHub) and len(github.created) == 1

    # every action was audited
    async with session(e2e_deps.engine) as s:
        row = await db.get_run(s, run_id)
        cost = await db.run_cost(s, run_id)
        tool_calls = len(await db.list_tool_calls(s, run_id))
        llm_calls = len(await db.list_llm_calls(s, run_id))
    assert row is not None and row.status == "done" and row.pr_url == final.pr_url
    assert tool_calls >= 3 and llm_calls >= 2
    assert float(row.cost_usd) == pytest.approx(cost.cost_usd, abs=1e-4)
    print(
        f"\nrun {run_id}: {report.total} tests, {tool_calls} tool calls, "
        f"{llm_calls} model turns, ${cost.cost_usd:.4f}"
    )
