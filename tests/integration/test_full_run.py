"""The whole M1 loop with everything real except the model.

Real Docker sandbox, real worktree and git, real Postgres rows, real push. The provider is
scripted so this proves the wiring without an API key or any spend. The end-to-end tests in
tests/e2e swap in a real model.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from contracts import Budget, Usage
from core.settings import Settings, load_settings
from gateway.openai_compat_provider import ChatTurn, OpenAICompatProvider, ToolCallReq
from orchestrator.deps import Deps, docker_sandbox_factory
from orchestrator.runner import run
from orchestrator.state import Phase, RunState
from repo.clone import repo_key
from repo.gitcmd import git
from storage import repo as db
from storage.db import make_engine, session
from storage.redis import RedisBus

pytestmark = pytest.mark.integration

IMAGE = "agent-sandbox:python-3.12"
FIXTURE_SRC = Path(__file__).resolve().parents[1] / "fixtures" / "fixture_repo"
GOAL = (
    "Implement subtract(a, b) and slugify(text) in fixture/ops.py so that "
    "tests/test_ops.py passes. Do not change the tests."
)
SOLUTION = """def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b


def slugify(text: str) -> str:
    return "-".join(text.lower().split())
"""


def _docker_ready() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
        return True
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_ready(), reason=f"docker or image {IMAGE} unavailable (run `make sandbox-image`)"
)


def call(cid: str, name: str, args: dict[str, Any]) -> ChatTurn:
    return ChatTurn(
        content=None,
        tool_calls=[ToolCallReq(cid, name, json.dumps(args))],
        finish_reason="tool_calls",
        usage=Usage(input_tokens=500, output_tokens=40, cost_usd=0.0),
        raw_message={"role": "assistant", "content": None},
    )


class ScriptedAgents(OpenAICompatProvider):
    """Plays every agent in the loop, dispatching on which tools it is offered.

    Sequencing by position breaks the moment a phase is added; dispatching on the
    submit tool in the request keeps the fake honest about what it is standing in for.
    """

    def __init__(self) -> None:
        super().__init__(model="scripted/agents", api_key=None, base_url="http://scripted")
        self.coder_script = [
            call("1", "str_replace_based_edit_tool", {"command": "view", "path": "."}),
            call("2", "str_replace_based_edit_tool", {"command": "view", "path": "fixture/ops.py"}),
            call(
                "3",
                "str_replace_based_edit_tool",
                {"command": "create", "path": "fixture/ops.py", "file_text": SOLUTION},
            ),
            call("4", "run_tests", {"selector": "tests/test_ops.py"}),
            call("5", "git_status", {}),
            call("6", "git_commit", {"message": "feat(ops): add subtract and slugify"}),
            call(
                "7",
                "submit_result",
                {
                    "summary": "Added subtract and slugify to fixture/ops.py",
                    "files_touched": ["fixture/ops.py"],
                    "how_to_test": "uv run pytest -q",
                    "notes_for_reviewer": [],
                },
            ),
            ChatTurn(
                "done",
                [],
                "stop",
                Usage(input_tokens=100, output_tokens=5),
                {"role": "assistant", "content": "done"},
            ),
        ]
        self.seen_tools: list[str] = []
        self.roles: list[str] = []

    async def _complete(self, **kw: Any) -> ChatTurn:
        offered = {t["function"]["name"] for t in kw.get("tools", [])}
        self.seen_tools = sorted(offered)
        for tool, role in (("submit_profile", "analyzer"), ("submit_plan", "planner")):
            if tool not in offered:
                continue
            if role in self.roles:  # submitted already: end the turn, as a real model would
                return self._done()
            self.roles.append(role)
            return call(role[0], tool, PROFILE if role == "analyzer" else PLAN)
        if "submit_result" in offered:
            if "coder" not in self.roles:
                self.roles.append("coder")
            return self.coder_script.pop(0)
        return self._done()

    @staticmethod
    def _done() -> ChatTurn:
        return ChatTurn("done", [], "stop", Usage(), {"role": "assistant", "content": "done"})

    async def parse(self, req: Any, output: type[Any]) -> tuple[Any, Usage]:
        self.roles.append("decomposer")
        return output.model_validate(TASK_GRAPH), Usage(input_tokens=200, output_tokens=40)


PROFILE = {
    "languages": ["python"],
    "framework": None,
    "package_manager": "uv",
    "test_command": "uv run --no-sync pytest -q",
    "lint_command": None,
    "conventions": ["tests live under tests/"],
    "entry_points": ["fixture/ops.py"],
}
PLAN = {
    "approach": "Implement the two missing functions.",
    "affected_files": ["fixture/ops.py"],
    "new_files": [],
    "risks": [],
    "test_strategy": "uv run --no-sync pytest -q tests/test_ops.py",
    "open_questions": [],
}
TASK_GRAPH = {
    "tasks": [
        {
            "id": "t1",
            "title": "Implement subtract and slugify",
            "description": "Add both functions to fixture/ops.py",
            "depends_on": [],
            "files": ["fixture/ops.py"],
            "acceptance_criteria": ["tests/test_ops.py passes"],
            "test_selector": "tests/test_ops.py",
        }
    ]
}


class StubGitHub:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def get_repo(self, full_name: str) -> StubGitHub:
        return self

    def get_pulls(self, **kw: Any) -> list[Any]:
        return []

    def create_pull(self, **kw: Any) -> Any:
        self.created.append(kw)
        return type("PR", (), {"html_url": "https://github.com/acme/demo/pull/7"})()


@pytest.fixture
def origin_repo(host_tmp: Path) -> Iterator[Path]:
    import asyncio

    src = host_tmp / "origin"
    src.mkdir()
    shutil.copytree(
        FIXTURE_SRC,
        src,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", ".pytest_cache"),
    )

    async def init() -> None:
        await git("init", "-q", "-b", "main", cwd=src)
        await git("add", "-A", cwd=src)
        await git("commit", "-q", "-m", "chore: fixture project", cwd=src)

    asyncio.run(init())
    yield src


@pytest.fixture
async def deps(
    migrated_pg_url: str, redis_url: str, host_tmp: Path
) -> AsyncIterator[tuple[Deps, ScriptedAgents, StubGitHub]]:
    settings: Settings = load_settings(env_file=None).model_copy(
        update={
            "database_url": migrated_pg_url,
            "redis_url": redis_url,
            "worktrees_dir": host_tmp / "worktrees",
            "repos_dir": host_tmp / "repos",
            "sandbox_image": IMAGE,
        }
    )
    provider, github = ScriptedAgents(), StubGitHub()
    engine: AsyncEngine = make_engine(migrated_pg_url, poolclass=NullPool)
    bus = RedisBus(redis_url)
    await bus.r.flushdb()
    d = Deps(
        settings=settings,
        provider=provider,
        engine=engine,
        bus=bus,
        sandbox_factory=docker_sandbox_factory(settings),
        github=github,
    )
    try:
        yield d, provider, github
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE runs CASCADE"))
        await bus.close()
        await engine.dispose()


@requires_docker
async def test_full_run_edits_tests_commits_pushes_and_opens_a_pr(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    d, provider, github = deps
    async with session(d.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(origin_repo),
            base_branch="main",
            goal=GOAL,
            budget=Budget(max_usd=1.0),
            provider="scripted",
        )
    state = RunState(
        run_id=run_id,
        goal=GOAL,
        repo_url=str(origin_repo),
        base_branch="main",
        work_branch=f"agent/{run_id}",
    )

    final = await run(state, d)

    assert final.phase is Phase.DONE, f"run failed: {final.error}; report={final.last_test_report}"
    assert final.task_result is not None
    assert final.task_result.files_touched == ["fixture/ops.py"]
    report = final.last_test_report
    assert report is not None and report.passed and report.total == 3 and report.failed == 0
    assert final.pr_url == "https://github.com/acme/demo/pull/7"

    # the coder was offered exactly its role's tools plus submit_result
    # every agent in the loop ran, in order, each offered its own tool set
    assert provider.roles == ["analyzer", "planner", "decomposer", "coder"]
    assert {"bash", "run_tests", "git_commit", "read_file", "search_code"} <= set(
        provider.seen_tools
    )

    # the branch reached the origin with the right content and untouched tests
    assert state.work_branch in await git("branch", "--list", state.work_branch, cwd=origin_repo)
    changed = (await git("diff", "--name-only", "main", state.work_branch, cwd=origin_repo)).split()
    assert changed == ["fixture/ops.py"]
    committed = await git("show", f"{state.work_branch}:fixture/ops.py", cwd=origin_repo)
    assert "def subtract" in committed and "def slugify" in committed
    author = await git("log", "-1", "--format=%an", state.work_branch, cwd=origin_repo)
    assert author.strip() == "autoswe[bot]"
    assert github.created[0]["head"] == state.work_branch and github.created[0]["base"] == "main"
    assert str(run_id) in github.created[0]["body"]

    # every action was audited and the run row agrees with the ledger
    async with session(d.engine) as s:
        row = await db.get_run(s, run_id)
        events = [e.type for e in await db.list_events(s, run_id)]
        artifact = await db.latest_artifact(s, run_id, "test_report")
        tool_names = [c.name for c in await db.list_tool_calls(s, run_id)]
        llm_turns = len(await db.list_llm_calls(s, run_id))
    assert row is not None and row.status == "done" and row.pr_url == final.pr_url
    assert row.started_at is not None and row.finished_at is not None
    # the audit trail replays the whole run in the order it happened, every agent included
    assert tool_names == [
        "submit_profile",  # analyzer
        "submit_plan",  # planner
        "str_replace_based_edit_tool",  # coder from here
        "str_replace_based_edit_tool",
        "str_replace_based_edit_tool",
        "run_tests",
        "git_status",
        "git_commit",
        "submit_result",
        "run_tests",  # the deterministic TEST phase after the coder finished
    ]
    assert llm_turns >= 8
    assert artifact is not None and artifact.content["passed"] is True
    # the stream tells the story of the run: each agent starting and finishing, the
    # phase changes between them, the test report, and the pull request at the end
    assert events[0] == "phase_changed"
    assert events[-1] in ("pr_opened", "run_finished")
    assert events.count("agent_started") == events.count("agent_finished") >= 4
    assert "test_report" in events and "pr_opened" in events

    # teardown really released everything
    assert not (Path(d.worktrees_dir()) / str(run_id)).exists()
    assert await d.bus.lock_owner(f"lock:repo:{repo_key(str(origin_repo))}:main") is None


@requires_docker
async def test_failing_tests_end_the_run_without_a_pr(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """The coder submits without fixing anything: TEST fails, so no branch is pushed."""
    d, provider, github = deps
    provider.coder_script = [
        call(
            "1",
            "submit_result",
            {
                "summary": "I could not work it out",
                "files_touched": [],
                "how_to_test": "pytest",
                "notes_for_reviewer": ["stuck"],
            },
        ),
        ChatTurn("done", [], "stop", Usage(), {"role": "assistant", "content": "done"}),
    ]
    async with session(d.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(origin_repo),
            base_branch="main",
            goal=GOAL,
            budget=Budget(),
            provider="scripted",
        )
    state = RunState(
        run_id=run_id,
        goal=GOAL,
        repo_url=str(origin_repo),
        base_branch="main",
        work_branch=f"agent/{run_id}",
    )

    final = await run(state, d)

    assert final.phase is Phase.FAILED and final.pr_url is None
    assert final.last_test_report is not None and not final.last_test_report.passed
    assert github.created == []
    assert state.work_branch not in await git("branch", "--list", cwd=origin_repo)
    async with session(d.engine) as s:
        row = await db.get_run(s, run_id)
    assert row is not None and row.status == "failed" and row.error


@requires_docker
async def test_a_second_run_is_blocked_by_the_repo_lock(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    d, _provider, _github = deps
    key = f"lock:repo:{repo_key(str(origin_repo))}:main"
    assert await d.bus.acquire_lock(key, "someone-else", 60)
    run_id = uuid.uuid4()
    async with session(d.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(origin_repo),
            base_branch="main",
            goal=GOAL,
            budget=Budget(),
            provider="scripted",
        )
    state = RunState(
        run_id=run_id,
        goal=GOAL,
        repo_url=str(origin_repo),
        base_branch="main",
        work_branch=f"agent/{run_id}",
    )
    with pytest.raises(RuntimeError, match="another run holds"):
        await run(state, d)
    assert await d.bus.lock_owner(key) == "someone-else"  # teardown did not steal it
