"""The whole M1 loop with everything real except the model.

Real Docker sandbox, real worktree and git, real Postgres rows, real push. The provider is
scripted so this proves the wiring without an API key or any spend. The end-to-end tests in
tests/e2e swap in a real model.
"""

from __future__ import annotations

import asyncio
import json
import shutil
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
from core.errors import RepoError
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
from tests.fakes import planted_secret

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
        if output.__name__ == "PullRequestDescription":
            self.roles.append("pr_writer")
            return output.model_validate(PR_DESCRIPTION), Usage(input_tokens=500, output_tokens=120)
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
PR_DESCRIPTION = {
    "title": "feat(ops): add subtract and slugify",
    "summary": "Adds the two functions tests/test_ops.py expects.",
    "changes": ["subtract returns a - b", "slugify lowercases and hyphenates"],
    # Deliberately wrong, in the direction a real writer errs: it says the suite is clean
    # without qualification. The rendered body carries the harness's counts beside this
    # line, so a reader can see the difference — which is the point of the split.
    "testing": "The whole suite passes.",
    # Deliberately non-empty while the run has none, and deliberately soft. `render` uses
    # the state's list, so neither survives into the body.
    "known_issues": ["nothing serious, will tidy up later"],
    "rollback": "Revert the merge commit.",
}
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
        self.labelled: list[str] = []

    def get_repo(self, full_name: str) -> StubGitHub:
        return self

    def get_pulls(self, **kw: Any) -> list[Any]:
        return []

    def create_pull(self, **kw: Any) -> Any:
        self.created.append(kw)
        stub = self

        class PR:
            html_url = "https://github.com/acme/demo/pull/7"

            def add_to_labels(self, *labels: str) -> None:
                # Applied after creation, because `create_pull` takes none. Recorded so the
                # test can tell "labels were applied" from "labelling failed and was
                # swallowed" — `open_pr` treats a label failure as best-effort, so without
                # this the two would look the same.
                stub.labelled.extend(labels)

        return PR()


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
        "pr_writer",  # last, with the branch already on the remote
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

    # ---- the pull request the run actually opened ----
    opened = github.created[0]
    assert opened["draft"] is False, "a clean run with nothing unresolved is not a draft"
    assert github.labelled == ["autoswe", "needs-review"]
    assert opened["title"] == "feat(ops): add subtract and slugify", "the writer's title"
    body = opened["body"]
    # The writer said "The whole suite passes." with no qualification, and wrote a soft
    # known-issues line the run does not have. Both are visible here as the split working:
    # the prose is rendered as written, the numbers are the harness's, and the state's
    # (empty) known-issues list is what reaches the section.
    assert "The whole suite passes." in body, "prose rendered as written"
    assert "3 passed, 0 failed" in body, "and the real counts beside it"
    assert "will tidy up later" not in body, "the model's invented known issue is not rendered"
    assert "## Known issues" in body and "None." in body
    assert "md5 used where a password hash is expected" in body, "the scan is in the body"
    assert "Pre-existing" in body, "and says the finding was not this run's"
    assert "opened the file: it predates this change" not in body, (
        "the model-written rationale stays in the artifact and off the forge"
    )
    assert str(run_id) in body

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
        # The PR Writer leaves no tool_calls row: it has no tools, by design, because its
        # output is published and a writer that could read files could quote one.
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


# The second task regresses its own test. `SOLUTION` (above) is what the first task
# commits — a green suite — and this is what every pass after it writes: `subtract` intact,
# `slugify` broken.
#
# It has to be this shape because of how TEST works. The selector is run first, but the
# full suite runs after it whenever the selector passes, so a task cannot finish while any
# other test is red. And the baseline cannot excuse the difference: the fixture's original
# `ops.py` defines only `add`, so the baseline run fails to *import* the test module and
# records a collection error rather than ids for `test_subtract` and `test_slugify`. That
# is what made the obvious version of this fake fail on task one, for a reason that had
# nothing to do with escalation.
BROKEN_SLUGIFY = """def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b


def slugify(text: str) -> str:
    return text
"""

TWO_TASKS = {
    "tasks": [
        {
            "id": "t1",
            "title": "Implement subtract and slugify",
            "description": "Add both to fixture/ops.py",
            "depends_on": [],
            "files": ["fixture/ops.py"],
            "acceptance_criteria": ["tests/test_ops.py passes"],
            "test_selector": "tests/test_ops.py",
        },
        {
            "id": "t2",
            "title": "Make slugify handle unicode",
            "description": "Extend slugify; it must keep passing its test",
            "depends_on": ["t1"],
            "files": ["fixture/ops.py"],
            "acceptance_criteria": ["tests/test_ops.py::test_slugify passes"],
            "test_selector": "tests/test_ops.py::test_slugify",
        },
    ]
}
# The replan of t2. One task with a fresh id, because `_replan_task` splices the
# replacements in where the old task was — handing it the original two-task graph would
# create a second `t1` and `by_id` would stop meaning anything.
REPLAN_T2 = {
    "tasks": [
        {
            "id": "t2.1",
            "title": "Handle unicode in slugify, in smaller steps",
            "description": "Normalise, then hyphenate",
            "depends_on": [],
            "files": ["fixture/ops.py"],
            "acceptance_criteria": ["tests/test_ops.py::test_slugify passes"],
            "test_selector": "tests/test_ops.py::test_slugify",
        }
    ]
}


class FinishesOneTaskThenStalls(ScriptedAgents):
    """Commits a finished, green first task, then breaks its own test on the second.

    Shaped this way because of how escalation interacts with Phase 3's rewind:
    `_rewind_task` resets the *failing* task to its own start sha before replanning, so a
    single-task run that exhausts has all of its commits discarded — correctly, since they
    are three abandoned half-fixes. An escalated run therefore has commits only when an
    earlier task succeeded, which is the case a draft pull request is actually for: work
    somebody can finish.

    The existing stuck-run test has a coder that touches nothing, so it escalates with an
    empty log and opens no pull request. That path is right and it is not this one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.passes = 0
        self.decomposed = False
        self.coder_script = []

    def _pass(self) -> list[ChatTurn]:
        self.passes += 1
        # The first pass finishes the work and leaves the suite green, so task one is
        # `done` and its commit is behind task two's start sha — which is what survives
        # the rewind when task two exhausts.
        content = SOLUTION if self.passes == 1 else BROKEN_SLUGIFY
        return [
            # The view is not padding. `create` refuses to overwrite a file the agent has
            # not read (`tools/editor.py::_create`), so without it the edit returns exit 1,
            # the file keeps only `add`, and the suite fails on an ImportError — which is
            # how this fake spent three runs failing for a reason unrelated to escalation.
            call(
                "0",
                "str_replace_based_edit_tool",
                {"command": "view", "path": "fixture/ops.py"},
            ),
            call(
                "1",
                "str_replace_based_edit_tool",
                {"command": "create", "path": "fixture/ops.py", "file_text": content},
            ),
            call("2", "git_commit", {"message": f"feat(ops): attempt {self.passes}"}),
            call(
                "3",
                "submit_result",
                {
                    "summary": f"attempt {self.passes}",
                    "files_touched": ["fixture/ops.py"],
                    "how_to_test": "uv run pytest -q",
                    "notes_for_reviewer": ["unicode handling is not right yet"],
                },
            ),
        ]

    async def parse(self, req: Any, output: type[Any]) -> tuple[Any, Usage]:
        if output.__name__ == "TaskGraphSpec":
            graph = TWO_TASKS if not self.decomposed else REPLAN_T2
            self.decomposed = True
            self.roles.append("decomposer")
            return output.model_validate(graph), Usage(input_tokens=200, output_tokens=40)
        return await super().parse(req, output)

    async def _complete(self, **kw: Any) -> ChatTurn:
        offered = {t["function"]["name"] for t in kw.get("tools", [])}
        # The Debugger is offered submit_result too; the parent plays it, and it
        # deliberately fixes nothing.
        if "submit_hypothesis" in offered:
            return await super()._complete(**kw)
        if "submit_result" in offered:
            if "coder" not in self.roles:
                self.roles.append("coder")
            # A fresh CODE phase arrives as [system, user] and nothing else. Refilling on
            # that, rather than whenever the script empties, is what stops this fake from
            # editing and submitting sixty times until `max_iterations` — which is not
            # what a model does, and the noise hid why this test was failing.
            if len(kw.get("messages") or []) <= 2:
                self.coder_script = self._pass()
            return self.coder_script.pop(0) if self.coder_script else self._done()
        return await super()._complete(**kw)


@requires_docker
async def test_an_escalated_run_with_commits_opens_a_draft_that_says_it_did_not_finish(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """The other half of the stuck path, and the one that matters to a human.

    A run that committed a finished task and then ran out of attempts on the next one has
    produced something somebody can pick up. Ending with the branch in a worktree that
    teardown deletes throws that away, so it is pushed and opened as a draft that says
    plainly it did not complete.

    Everything here is real except the model: a real worktree, real commits, a real push to
    the origin repository — and the run still ends `failed`.
    """
    d, _, github = deps
    provider = FinishesOneTaskThenStalls()
    d = replace(d, provider=provider)
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

    assert final.phase is Phase.FAILED, "it failed, and that is not in question"
    assert final.error, "with its own diagnosis"
    assert final.pr_url, f"but the work is reachable: {final.error}"

    # the branch really reached the origin, with the finished task's commit on it
    assert state.work_branch in await git("branch", "--list", state.work_branch, cwd=origin_repo)
    log = await git("log", "--oneline", f"main..{state.work_branch}", cwd=origin_repo)
    assert "feat(ops): attempt" in log, log
    committed = await git("show", f"{state.work_branch}:fixture/ops.py", cwd=origin_repo)
    assert "return a - b" in committed, "the task that succeeded is in the branch"

    (opened,) = github.created
    assert opened["draft"] is True
    assert opened["title"].startswith("[WIP] "), "where a list view and an email read it"
    body = opened["body"]
    assert "This run did not complete:" in body
    assert "## What was already tried" in body, "the hypotheses are why this draft is useful"

    # `pr_url` survives the run finishing as failed. The runner passes it through now;
    # without that, the only pointer to the pushed work would be written over with NULL.
    async with session(d.engine) as s:
        row = await db.get_run(s, run_id)
        artifacts = [a.kind for a in await db.list_artifacts(s, run_id)]
    assert row is not None
    assert row.status == "failed" and row.pr_url == final.pr_url
    assert row.error == final.error, "the run's failure, not a pull-request failure"
    assert "pr" in artifacts, "and the body that was published is on the record"


requires_gitleaks = pytest.mark.skipif(
    not shutil.which("gitleaks"), reason="gitleaks is not on PATH"
)


class CommitsASecret(ScriptedAgents):
    """A coder that finishes the work correctly and also commits a credential.

    The tests pass, the review is clean, and the stubbed scanner pass finds nothing — so
    the run reaches PR believing it is done. The only thing standing between that
    credential and a public forge is the rescan in `_refuse_secrets`, which is what this
    exercises with the real gitleaks binary rather than a stub.
    """

    def __init__(self) -> None:
        super().__init__()
        self.coder_script = [
            call("1", "str_replace_based_edit_tool", {"command": "view", "path": "fixture/ops.py"}),
            call(
                "2",
                "str_replace_based_edit_tool",
                {"command": "create", "path": "fixture/ops.py", "file_text": SOLUTION},
            ),
            # A new file, so no prior view is required — and a plausible place for a key
            # to end up, which is the point.
            call(
                "3",
                "str_replace_based_edit_tool",
                {
                    "command": "create",
                    "path": "fixture/settings.py",
                    "file_text": f'AWS_SECRET_ACCESS_KEY = "{planted_secret("full-run")}"\n',
                },
            ),
            call("4", "run_tests", {"selector": "tests/test_ops.py"}),
            call("5", "git_commit", {"message": "feat(ops): add subtract and slugify"}),
            call(
                "6",
                "submit_result",
                {
                    "summary": "Added both functions and a settings module",
                    "files_touched": ["fixture/ops.py", "fixture/settings.py"],
                    "how_to_test": "uv run pytest -q",
                    "notes_for_reviewer": [],
                },
            ),
            ChatTurn("done", [], "stop", Usage(), {"role": "assistant", "content": "done"}),
        ]


@requires_docker
@requires_gitleaks
async def test_a_committed_secret_stops_the_push_with_the_real_scanner(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """Exit criterion: a planted `AWS_SECRET_ACCESS_KEY=...` in a new file refuses the push.

    The gate is unit-tested against a stubbed scanner in tests/unit/test_pr_node.py. This
    is the other half: the real gitleaks binary, over a real commit in a real worktree,
    reached through the whole pipeline. The stubbed `run_all` in this module does not reach
    it — `_refuse_secrets` calls `run_gitleaks` directly, which is the defence-in-depth
    rescan immediately before the push.

    Asserted by absence as much as presence: the branch must not exist on the origin.
    """
    d, _, github = deps
    d = replace(d, provider=CommitsASecret())
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

    with pytest.raises(RepoError, match="refusing to push"):
        await run(state, d)

    assert state.pr_url is None
    assert github.created == [], "no pull request was opened"
    assert state.work_branch not in await git(
        "branch", "--list", state.work_branch, cwd=origin_repo
    ), "and the branch never reached the origin"

    # the finding is on the record for a human to rotate, and the value is not
    async with session(d.engine) as s:
        row = await db.get_run(s, run_id)
        artifact = await db.latest_artifact(s, run_id, "gitleaks")
    assert row is not None and row.status == "failed"
    assert artifact is not None, "the run that refused to push said what it found"
    (found,) = artifact.content["findings"]
    assert found["tool"] == "gitleaks"
    assert found["file"] == "fixture/settings.py"
    assert found["severity"] == "critical"
    assert "value withheld" in found["message"]
    assert planted_secret("full-run") not in json.dumps(artifact.content)


BLOCKING_REVIEW = {
    "findings": [
        {
            "file": "fixture/ops.py",
            "line": 9,
            "severity": "blocking",
            "category": "missing-validation",
            "summary": "slugify does not reject None",
            "failure_scenario": "slugify(None) raises AttributeError instead of TypeError",
        }
    ],
    "blocking": True,
    "rejections": [],
}


class ReviewBlocksThenClears(ScriptedAgents):
    """A reviewer that blocks once, then passes. The coder is asked twice.

    The fix loop has table tests over the real `transition`, but nothing drove it through
    an actual run: no test made a review return `blocking=True` and then watched a fix task
    go through CODE and TEST and come back to the gate that asked for it.
    """

    reviews_before_clean = 1

    def __init__(self) -> None:
        super().__init__()
        self.reviews = 0
        self.review_submitted = False
        self.first_pass = list(self.coder_script)
        self.coder_script = []

    def _pass(self) -> list[ChatTurn]:
        if self.first_pass:
            script, self.first_pass = self.first_pass, []
            return script
        # The fix round. It submits without editing: the suite is already green, and what
        # is under test is the loop rather than the quality of the fix.
        return [
            call(
                "f",
                "submit_result",
                {
                    "summary": "addressed the review finding",
                    "files_touched": ["fixture/ops.py"],
                    "how_to_test": "uv run pytest -q",
                    "notes_for_reviewer": ["slugify now rejects None"],
                },
            )
        ]

    async def _complete(self, **kw: Any) -> ChatTurn:
        offered = {t["function"]["name"] for t in kw.get("tools", [])}
        if "submit_review" in offered:
            # Counted per *phase*, not per call. A fresh phase arrives as [system, user];
            # within one phase the loop keeps asking, and a fake that answers every time
            # both miscounts the rounds and re-submits until `max_iterations` — which is
            # not what a model does. Same trap as the coder script below.
            if len(kw.get("messages") or []) <= 2:
                self.reviews += 1
                self.review_submitted = False
                self.roles.append("review")
            if self.review_submitted:
                return self._done()
            self.review_submitted = True
            blocking = self.reviews <= self.reviews_before_clean
            return call("v", "submit_review", BLOCKING_REVIEW if blocking else REVIEW_REPORT)
        if "submit_security" in offered:
            if "security" in self.roles:
                return self._done()
            self.roles.append("security")
            return call("s", "submit_security", SECURITY_REPORT)
        if "submit_result" in offered:
            if "coder" not in self.roles:
                self.roles.append("coder")
            if len(kw.get("messages") or []) <= 2:
                self.coder_script = self._pass()
            return self.coder_script.pop(0) if self.coder_script else self._done()
        return await super()._complete(**kw)


class ReviewNeverClears(ReviewBlocksThenClears):
    """Blocks every time, so the round budget runs out and the findings go on the record."""

    reviews_before_clean = 99


async def _run_to_completion(d: Deps, origin_repo: Path) -> tuple[RunState, Any]:
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
        unattended=True,
    )
    return await run(state, d), run_id


@requires_docker
async def test_a_blocking_review_sends_a_fix_task_through_code_and_test(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """Exit criterion: blocking findings -> CODE (fix task) -> TEST -> REVIEW again.

    Walked by a real run rather than by the transition table alone: the fix task is
    appended by `_grant_fix_round`, picked up by `code_node` as an ordinary task, tested,
    and returned to REVIEW by the `return_to` hop.
    """
    d, _, github = deps
    d = replace(d, provider=ReviewBlocksThenClears())

    final, run_id = await _run_to_completion(d, origin_repo)

    assert final.phase is Phase.DONE, final.error
    assert final.fix_rounds == {"review": 1}, "one round bought, one spent"
    assert final.tasks is not None
    fix = final.tasks.by_id("fix-review-1")
    assert fix.kind == "fix" and fix.status == "done", "an ordinary task, tested like any"
    assert final.known_issues == [], "the second review was clean, so nothing was left"
    assert final.review is not None and final.review.blocking is False

    async with session(d.engine) as s:
        steps = [st.agent for st in await db.list_steps(s, run_id)]
    # the reviewer ran twice, with a coder between them
    assert steps.count("review") == 2, steps
    assert steps.index("coder") < steps.index("review"), "coded before the first review"
    assert steps.count("coder") == 2, "and again for the fix round"
    assert github.created[0]["draft"] is False, "a run that fixed its findings is not a draft"


@requires_docker
async def test_a_review_that_never_clears_proceeds_with_the_findings_on_the_record(
    deps: tuple[Deps, ScriptedAgents, StubGitHub], origin_repo: Path
) -> None:
    """Exit criterion: after two rounds the run proceeds with `known_issues`.

    The budget is what makes this terminate. A pull request that names what is wrong with
    it beats one that never arrives — and it is a draft, so nobody mistakes it for ready.
    """
    d, _, github = deps
    d = replace(d, provider=ReviewNeverClears())

    final, _run_id = await _run_to_completion(d, origin_repo)

    assert final.phase is Phase.DONE, final.error
    assert final.fix_rounds == {"review": 2}, "two rounds, and no third"
    assert final.known_issues, "the unresolved finding is on the record"
    assert "slugify does not reject None" in final.known_issues[0]

    (opened,) = github.created
    assert opened["draft"] is True
    assert "slugify does not reject None" in opened["body"], "and in the body a human reads"
