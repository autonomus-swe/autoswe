"""The whole M1 loop with everything real except the model.

Real Docker sandbox, real worktree and git, real Postgres rows, real push. The provider is
scripted so this proves the wiring without an API key or any spend. The end-to-end tests in
tests/e2e swap in a real model.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from agents import security
from contracts import Budget, SecurityFinding, Usage
from contracts.plan import ImplementationPlan, TaskGraph
from contracts.repo import RepoProfile
from core.settings import Settings, load_settings
from gateway.openai_compat_provider import ChatTurn, OpenAICompatProvider, ToolCallReq
from orchestrator.deps import Deps, docker_sandbox_factory
from orchestrator.runner import run
from orchestrator.state import Phase, RunState
from orchestrator.transition import MAX_DEBUG_ATTEMPTS
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
        # Accumulated across the run, not overwritten per call: the question is "was this
        # tool ever offered", and the last caller used to be the coder and is now the
        # reviewer, whose set is deliberately read-only.
        self.seen_tools: set[str] = set()
        self.roles: list[str] = []
        self.hypothesis_submitted = False

    async def _complete(self, **kw: Any) -> ChatTurn:
        offered = {t["function"]["name"] for t in kw.get("tools", [])}
        self.seen_tools |= offered
        for tool, role in (("submit_profile", "analyzer"), ("submit_plan", "planner")):
            if tool not in offered:
                continue
            if role in self.roles:  # submitted already: end the turn, as a real model would
                return self._done()
            self.roles.append(role)
            return call(role[0], tool, PROFILE if role == "analyzer" else PLAN)
        # The Debugger is the only role offered submit_hypothesis. It states a
        # hypothesis, then submits a result that fixes nothing, so the loop exhausts its
        # attempts the way a genuinely stuck run does.
        if "submit_hypothesis" in offered:
            self.roles.append("debugger")
            if not self.hypothesis_submitted:
                self.hypothesis_submitted = True
                return call("h", "submit_hypothesis", HYPOTHESIS)
            self.hypothesis_submitted = False
            return call("r", "submit_result", STUCK_RESULT)
        if "submit_review" in offered:
            # Guarded like the analyzer and planner above: submit once, then end the turn.
            # Without this the fake re-submits every turn until max_iterations, which is
            # what a real model does not do — and it hides how many turns a phase took.
            if "review" in self.roles:
                return self._done()
            self.roles.append("review")
            return call("v", "submit_review", REVIEW_REPORT)
        if "submit_security" in offered:
            if "security" in self.roles:
                return self._done()
            self.roles.append("security")
            return call("s", "submit_security", SECURITY_REPORT)
        if "submit_result" in offered:
            if "coder" not in self.roles:
                self.roles.append("coder")
            # An exhausted script means this fake has nothing left to say, which is a
            # turn that ends rather than an IndexError halfway through a run.
            return self.coder_script.pop(0) if self.coder_script else self._done()
        return self._done()

    @staticmethod
    def _done() -> ChatTurn:
        return ChatTurn("done", [], "stop", Usage(), {"role": "assistant", "content": "done"})

    async def parse(self, req: Any, output: type[Any]) -> tuple[Any, Usage]:
        # Two roles reach `parse` now. Dispatching on the requested type rather than on
        # call order keeps this fake honest when a phase is inserted — which is exactly
        # what happened when REVIEW arrived between TEST and PR.
        if output.__name__ == "ReviewCandidates":
            self.roles.append("review_pre")
            return output.model_validate(REVIEW_CANDIDATES), Usage(
                input_tokens=400, output_tokens=60
            )
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
HYPOTHESIS = {
    "failure_class": "assertion",
    "root_cause": "the implementation was never written",
    "plan": "write it",
    "confidence": 0.6,
}
STUCK_RESULT = {
    "summary": "still cannot work it out",
    "files_touched": [],
    "how_to_test": "pytest",
    "notes_for_reviewer": ["stuck"],
}
# The pre-pass raises one real concern; the verification pass confirms it as a `minor`, so
# the run is reviewed and not blocked — fix rounds are a later change.
REVIEW_CANDIDATES = {
    "findings": [
        {
            "file": "fixture/ops.py",
            "line": 5,
            "severity": "minor",
            "category": "missing-docstring",
            "summary": "subtract has no docstring",
            "failure_scenario": "a reader has to infer the argument order from the body",
        }
    ]
}
REVIEW_REPORT = {
    "findings": REVIEW_CANDIDATES["findings"],
    "blocking": False,
}
# The scanners themselves are exercised against a real sandbox in
# tests/integration/test_scanners.py. Here they are stubbed, for two reasons. Their output
# depends on the rule packs baked into the image, so this test would assert whatever semgrep
# happens to think today; and a `high` finding inside the diff would *correctly* grant a fix
# round and send the run back to CODE, which would make the audit trail below a function of
# a vendored rule pack. What this test is for is the pipeline: that SECURITY runs between
# REVIEW and PR, and that what it decides reaches the record.
SCANNER_FINDINGS = [
    # Inherited: worth listing, and not this run's to answer for.
    SecurityFinding(
        tool="bandit",
        rule="B324",
        file="fixture/legacy.py",
        line=12,
        severity="high",
        message="md5 used where a password hash is expected",
        verified_by_llm=False,
        false_positive=False,
        rationale="",
        in_diff=False,
    ),
    # A scanner that could not run says so, and an `info` finding cannot gate.
    SecurityFinding(
        tool="pip-audit",
        rule="scan-failed",
        file="",
        line=0,
        severity="info",
        message="pip-audit did not run: the fixture has no lockfile",
        verified_by_llm=False,
        false_positive=False,
        rationale="",
        in_diff=False,
    ),
]
SECURITY_REPORT = {
    "findings": [
        {
            **f.model_dump(mode="json"),
            "verified_by_llm": True,
            "rationale": "opened the file: it predates this change",
        }
        for f in SCANNER_FINDINGS
    ],
    # Deliberately wrong. Neither finding can gate — one is outside the diff, the other is
    # `info` — so the run reaching DONE is the end-to-end proof that the harness recomputes
    # this rather than reading the model's answer.
    "critical": True,
    "checklist": dict.fromkeys(security.CHECKLIST_KEYS, True),
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


@pytest.fixture(autouse=True)
def _deterministic_scanners(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stubbed for every run in this module. See SCANNER_FINDINGS for why."""

    async def run_all(*a: Any, **kw: Any) -> list[SecurityFinding]:
        return list(SCANNER_FINDINGS)

    monkeypatch.setattr("tools.scanners.run_all", run_all)


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
    assert provider.roles == [
        "analyzer",
        "planner",
        "decomposer",
        "coder",
        "review_pre",  # the cheap pass enumerates
        "review",  # the expensive one verifies
        "security",  # and the scan runs before anything is pushed
    ]
    assert {"bash", "run_tests", "git_commit", "read_file", "search_code"} <= provider.seen_tools
    # and neither read-only role was offered a way to change anything
    assert {"submit_review", "submit_security", "git_log"} <= provider.seen_tools
    # The fake submitted `critical: true`; the harness recomputed it from the severities.
    # One finding is outside the diff and the other is `info`, so neither can gate — and
    # the run reaching DONE is what proves the model's own boolean was not read.
    assert final.security is not None
    assert final.security.critical is False, "the model said critical and the harness disagreed"
    assert [f.tool for f in final.security.findings if f.tool != "checklist"] == [
        "bandit",
        "pip-audit",
    ]

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
        "run_tests",  # the baseline, once the analyzer has settled the test command
        "submit_plan",  # planner
        "str_replace_based_edit_tool",  # coder from here
        "str_replace_based_edit_tool",
        "str_replace_based_edit_tool",
        "run_tests",
        "git_status",
        "git_commit",
        "submit_result",
        # the deterministic TEST phase after the coder finished: the task's own tests
        # first, then the whole suite to prove nothing else broke. Both are recorded —
        # one row per run, since "which run produced this" is the question a reader has.
        "run_tests",
        "run_tests",
        # the reviewer's verification pass. The cheap pass before it has no tools at all,
        # so it leaves an llm_calls row and no tool_calls row.
        "submit_review",
        # the security pass, between the review and the push
        "submit_security",
    ]
    assert llm_turns >= 8
    assert artifact is not None and artifact.content["passed"] is True
    # the stream tells the story of the run: each agent starting and finishing, the
    # phase changes between them, the test report, and the pull request at the end
    assert events[0] == "phase_changed"
    assert events[-1] == "run_finished"
    assert events.count("agent_started") == events.count("agent_finished") >= 4
    # every type a viewer relies on is on the stream, not just the ones easy to assert
    assert {
        "phase_changed",
        "agent_started",
        "agent_finished",
        "tool_call",
        "test_report",
        "security_report",
        "pr_opened",
        "run_finished",
    } <= set(events), sorted(set(events))
    # Every tool call an *agent* made is on the stream as tool_call. Three of the rows
    # above were not an agent's: the baseline and the two runs in TEST. Those announce
    # themselves as test_report — one per phase that reports, not one per run.
    DETERMINISTIC_TEST_RUNS = 3
    assert events.count("tool_call") == len(tool_names) - DETERMINISTIC_TEST_RUNS
    assert events.count("test_report") == 2, "the baseline, and the verdict on the task"

    # teardown really released everything
    assert not (Path(d.worktrees_dir()) / str(run_id)).exists()
    assert await d.bus.lock_owner(f"lock:repo:{repo_key(str(origin_repo))}:main") is None


@requires_docker
async def test_failing_tests_end_the_run_without_a_pr(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """A stuck run exhausts its debug attempts and escalates, and still opens no PR.

    Phase 3 changed what this path looks like. A failing test used to end the run; now it
    goes to DEBUG, and only after the attempts and a replan are spent does the run fail.
    Unattended, because attended it would park on a human instead of terminating.
    """
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
        unattended=True,
    )

    final = await run(state, d)

    assert final.phase is Phase.FAILED and final.pr_url is None
    assert final.last_test_report is not None and not final.last_test_report.passed
    assert github.created == []
    assert state.work_branch not in await git("branch", "--list", cwd=origin_repo)
    async with session(d.engine) as s:
        row = await db.get_run(s, run_id)
        steps = await db.list_steps(s, run_id)
        events = [e.type for e in await db.list_events(s, run_id)]
    assert row is not None and row.status == "failed" and row.error

    # it debugged before giving up, and each attempt recorded its hypothesis
    debug_steps = [st for st in steps if st.agent == "debugger"]
    assert debug_steps, [st.agent for st in steps]
    assert all((st.output or {}).get("hypothesis") for st in debug_steps)
    assert "debug_hypothesis" in events and "escalated" in events
    # and it stopped rather than looping: the cap is what ends it
    assert max(state.attempts.values()) >= MAX_DEBUG_ATTEMPTS, state.attempts


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


@requires_docker
async def test_the_plan_and_task_graph_land_where_phase_3_will_read_them(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """Phase 3 reads steps.output and the tasks table, not the in-memory state."""
    d, _, _ = deps
    async with session(d.engine) as s:
        run_id = await db.create_run(
            s,
            repo_url=str(origin_repo),
            base_branch="main",
            goal=GOAL,
            budget=Budget(max_usd=1.0),
            provider="scripted",
        )
    final = await run(
        RunState(
            run_id=run_id,
            goal=GOAL,
            repo_url=str(origin_repo),
            base_branch="main",
            work_branch=f"agent/{run_id}",
        ),
        d,
    )
    assert final.phase is Phase.DONE, final.error

    async with session(d.engine) as s:
        steps = await db.list_steps(s, run_id)
        tasks = await db.list_tasks(s, run_id)

    by_agent = {st.agent: st for st in steps}
    # each single-shot agent stored a result that still validates against its contract
    RepoProfile.model_validate(by_agent["analyzer"].output)
    plan = ImplementationPlan.model_validate(by_agent["planner"].output)
    assert plan.test_strategy and plan.affected_files == PLAN["affected_files"]
    # the decomposer stores the runtime graph, so per-task status is part of the record
    graph = TaskGraph.model_validate(by_agent["decomposer"].output)
    assert [t.spec.id for t in graph.tasks] == [t["id"] for t in TASK_GRAPH["tasks"]]

    # and the graph was projected into the tasks table the API and UI read
    assert [t.id for t in tasks] == [t["id"] for t in TASK_GRAPH["tasks"]]
    assert all(t.status == "done" for t in tasks), [(t.id, t.status) for t in tasks]
    assert tasks[0].test_selector == TASK_GRAPH["tasks"][0]["test_selector"]
    assert tasks[0].acceptance_criteria == TASK_GRAPH["tasks"][0]["acceptance_criteria"]


class AsksOnceThenPlans(ScriptedAgents):
    """The Planner raises an open question the first time, and plans properly the second.

    The tool loop calls ``_complete`` repeatedly inside a single planner run, so counting
    calls submits twice and the second answer quietly overwrites the first. Dispatch on
    what the conversation already shows instead, as the parent class does.
    """

    def __init__(self) -> None:
        super().__init__()
        self.plans = 0

    async def _complete(self, **kw: Any) -> ChatTurn:
        offered = {t["function"]["name"] for t in kw.get("tools", [])}
        if "submit_plan" not in offered:
            return await super()._complete(**kw)
        # A tool result in the history means this run has already submitted, so stop as a
        # real model would. `call()` leaves tool_calls out of raw_message, so looking for
        # the submission itself finds nothing and the run submits twice — the second,
        # question-free plan silently replacing the first.
        if any(m.get("role") == "tool" for m in kw.get("messages", [])):
            return self._done()
        self.plans += 1
        if self.plans == 1:
            self.roles.append("planner")
            return call("p1", "submit_plan", {**PLAN, "open_questions": ["Which scheme?"]})
        return call(f"p{self.plans}", "submit_plan", PLAN)


@requires_docker
async def test_an_open_question_pauses_the_run_and_the_answer_is_on_the_record(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """The re-plan has to show what the human said, or it looks like the first one.

    Without this the audit trail cannot distinguish the plan that asked the question from
    the plan that acted on the answer.
    """
    answer = "JWT with HS256, secret from env JWT_SECRET."
    d, _, _ = deps
    d = replace(d, provider=AsksOnceThenPlans())
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

    task = asyncio.create_task(run(state, d))
    try:
        # Wait for the event, not the run's status. awaiting_input_node sets the status
        # first and emits afterwards, so polling the status and then reading events is a
        # race that only shows up when the machine is loaded enough to widen the gap.
        asked: list[Any] = []
        for _ in range(240):
            await asyncio.sleep(0.5)
            async with session(d.engine) as s:
                asked = [e for e in await db.list_events(s, run_id) if e.type == "awaiting_input"]
            if asked:
                break
            if task.done():
                done = await task  # surface whatever went wrong instead of timing out
                pytest.fail(
                    f"the run finished without pausing: phase={done.phase}, "
                    f"open_questions={done.plan.open_questions if done.plan else None}"
                )
        else:
            pytest.fail("run never announced an open question")

        assert asked[-1].payload["questions"] == ["Which scheme?"]
        async with session(d.engine) as s:
            parked = await db.get_run(s, run_id)
        assert parked is not None and parked.status == "awaiting_input", (
            "the status is set before the event, so by now it must agree"
        )

        await d.bus.push_inbox(run_id, {"type": "answer", "text": answer})
        final = await asyncio.wait_for(task, timeout=600)
    finally:
        if not task.done():
            task.cancel()

    assert final.phase is Phase.DONE, final.error
    assert final.answers == [("Which scheme?", answer)]
    assert final.waiting_s > 0, "time parked on a human is not agent time"

    async with session(d.engine) as s:
        steps = await db.list_steps(s, run_id)
    planners = [st for st in steps if st.agent == "planner"]
    assert len(planners) == 2, [st.agent for st in steps]
    # the first plan asked and could not have known the answer; the second is driven by it
    assert "answers" not in (planners[0].input or {})
    assert (planners[1].input or {})["answers"] == [
        {"questions": "Which scheme?", "answer": answer}
    ]


class StallsThenLoops(ScriptedAgents):
    """A coder that never submits, so the run stays inside one node's tool loop."""

    async def _complete(self, **kw: Any) -> ChatTurn:
        # Only the coder's behaviour differs; every other role falls through to the parent,
        # which is the one place that decides what each submits. This used to carry its own
        # copy of the review branch, which was a second place for that to drift — and would
        # have needed a third copy when SECURITY arrived.
        offered = {t["function"]["name"] for t in kw.get("tools", [])}
        if "submit_result" in offered:
            return call("x", "str_replace_based_edit_tool", {"command": "view", "path": "."})
        return await super()._complete(**kw)


@requires_docker
async def test_cancel_stops_a_run_inside_a_tool_loop_and_removes_the_container(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """A node boundary is not good enough: a coder loop can run for minutes."""
    import docker

    d, _, _ = deps
    d = replace(d, provider=StallsThenLoops())
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

    task = asyncio.create_task(run(state, d))
    try:
        # let it get past SETUP and into the coder's loop, then pull the plug
        for _ in range(240):
            await asyncio.sleep(0.5)
            async with session(d.engine) as s:
                row = await db.get_run(s, run_id)
            if row is not None and row.phase == Phase.CODE.value:
                break
        else:
            pytest.fail("run never reached CODE")
        await d.bus.set_cancel(run_id)
        final = await asyncio.wait_for(task, timeout=120)
    finally:
        if not task.done():
            task.cancel()

    assert final.cancelled and final.phase is Phase.FAILED
    async with session(d.engine) as s:
        row = await db.get_run(s, run_id)
    assert row is not None, "the run row vanished"
    assert row.status == "cancelled", row.status

    # the stream says so, and the sandbox is gone rather than left running
    async with session(d.engine) as s:
        events = [e.type for e in await db.list_events(s, run_id)]
    assert events[-1] == "run_finished"
    client = docker.from_env()
    assert not client.containers.list(all=True, filters={"name": f"run-{run_id}"})
    assert not d.settings.keep_failed_sandbox, "the removal above assumes the default"
    # the worktree is kept on purpose: nothing was pushed, so the work is still
    # recoverable. teardown only removes it once the branch is safely on the remote.
    assert (Path(d.worktrees_dir()) / str(run_id)).exists()
