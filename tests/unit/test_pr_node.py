"""The PR phase: scan, push, describe, open — in that order, for reasons.

The order is the design, and each step is placed by what cannot be undone:

- **The secret scan is before the push**, because a push cannot be recalled. It is also
  re-run here rather than trusted from the SECURITY phase: a fix round can introduce a
  secret after the scan that cleared the run, and `security_node` records an empty report
  when there is no sandbox — yet gitleaks runs on the worker and needs none, so a resumed
  run that lost its sandbox would otherwise push having never been scanned for secrets.
- **The push is before the writer**, because a branch on the remote is worth having even
  if a prose model is unavailable.
- **The description is written from facts the harness already holds**, so a writer that
  fails costs prose and nothing else.

These tests assert the *absence* of effects as much as their presence: for the refusal, the
thing that matters is that `push_branch` was never reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from contracts import (
    Budget,
    PullRequestDescription,
    SecurityFinding,
    SecurityReport,
    Task,
    TaskGraph,
    TaskSpec,
    TestReport,
    Usage,
)
from core.errors import RepoError
from gateway.routing import ROUTES
from orchestrator.nodes import RunResources, pr_node
from orchestrator.state import Phase, RunState
from repo.worktree import Worktree
from tests.fakes import FakeProviderBase, planted_secret

pytestmark = pytest.mark.unit

DESCRIPTION = PullRequestDescription(
    title="feat(ops): add subtract",
    summary="Adds subtract.",
    changes=["subtract returns a - b"],
    testing="41 passed.",
    known_issues=[],
    rollback="Revert the merge commit.",
)


def spec(task_id: str = "t1") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title="add subtract",
        description="d",
        depends_on=[],
        files=["src/a.py"],
        acceptance_criteria=["works"],
        test_selector="tests/",
    )


def report(passed: bool = True) -> TestReport:
    return TestReport(
        passed=passed,
        total=41,
        failed=0 if passed else 3,
        errors=0,
        skipped=0,
        failures=[],
        duration_s=2.5,
        command="uv run pytest -q",
        truncated_output="",
        signature="s",
    )


def leak(file: str = "config.py", line: int = 7, message: str | None = None) -> SecurityFinding:
    return SecurityFinding(
        tool="gitleaks",
        rule="generic-api-key",
        file=file,
        line=line,
        severity="critical",
        message=message or f"a key was committed in {file} (value withheld)",
        verified_by_llm=True,
        false_positive=False,
        rationale="confirmed against the commit",
        in_diff=True,
    )


def state(**kw: Any) -> RunState:
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "goal": "Implement subtract",
        "repo_url": "https://github.com/a/b",
        "base_branch": "main",
        "work_branch": "agent/x",
        "phase": Phase.PR,
        "tasks": TaskGraph(tasks=[Task(spec=spec(), status="done")]),
        "budget": Budget(),
        "last_test_report": report(),
    }
    return RunState(**{**base, **kw})


class FakeProvider(FakeProviderBase):
    def __init__(self, result: Any = DESCRIPTION) -> None:
        self.result = result
        self.calls = 0

    async def parse(self, req: Any, output: type[Any]) -> tuple[Any, Usage]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, Usage()


class FakeDeps:
    def __init__(self, provider: Any = None) -> None:
        self.engine = None
        self.bus = None
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.provider = provider or FakeProvider()
        self.settings = None
        self.github = object()

    def git_token(self) -> str | None:
        return None


class Recorder:
    """What left the machine, and with what. Empty is the assertion in the refusal tests."""

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.prs: list[dict[str, Any]] = []
        self.artifacts: list[tuple[str, dict[str, Any]]] = []
        self.gitleaks: list[SecurityFinding] = []
        self.order: list[str] = []


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Everything with an outside effect, stubbed and recorded — including the order."""
    import orchestrator.nodes as nodes

    rec = Recorder()

    async def run_gitleaks(worktree: Path, base_sha: str) -> list[SecurityFinding]:
        rec.order.append("scan")
        return list(rec.gitleaks)

    async def push_branch(worktree: Path, branch: str, token: str | None = None) -> None:
        rec.order.append("push")
        rec.pushed.append(branch)

    async def open_pr(repo_url: str, **kw: Any) -> str:
        rec.order.append("open_pr")
        rec.prs.append(kw)
        return "https://github.com/a/b/pull/7"

    async def emit(deps: Any, run_id: Any, type: str, payload: dict[str, Any]) -> None:
        deps.events.append((type, payload))

    async def gitcmd(*a: Any, **kw: Any) -> str:
        return " src/a.py | 2 +-"

    async def begin(
        st: Any, deps: Any, res: Any, agent: str, phase: Any
    ) -> tuple[Any, Any, Any, Any]:
        rec.order.append(f"begin:{agent}")
        return uuid4(), None, None, ROUTES[agent]

    async def end(*a: Any, **kw: Any) -> None:
        return None

    async def save_artifact(s: Any, run_id: Any, kind: str, path: Any, content: Any) -> None:
        rec.artifacts.append((kind, content))

    class NullSession:
        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def noop(*a: Any, **kw: Any) -> None:
        return None

    # Patched on the module, not on `nodes`: `nodes` does `from tools import scanners`
    # and looks the function up at call time, so this is the same object it will reach.
    monkeypatch.setattr("tools.scanners.run_gitleaks", run_gitleaks)
    monkeypatch.setattr(nodes, "push_branch", push_branch)
    monkeypatch.setattr(nodes, "open_pr", open_pr)
    monkeypatch.setattr(nodes, "_emit", emit)
    monkeypatch.setattr(nodes, "git", gitcmd)
    monkeypatch.setattr(nodes, "_begin", begin)
    monkeypatch.setattr(nodes, "_end", end)
    monkeypatch.setattr(nodes, "session", lambda _engine: NullSession())
    monkeypatch.setattr("storage.repo.save_artifact", save_artifact)
    monkeypatch.setattr("storage.repo.finish_run", noop)
    return rec


def resources(tmp_path: Path) -> RunResources:
    return RunResources(
        worktree=Worktree(path=tmp_path, branch="agent/x", bare=tmp_path, run_id="r")
    )


# ---- the order ----------------------------------------------------------------------------


async def test_the_scan_runs_before_the_push_and_the_writer_after_it(
    tmp_path: Path, sent: Recorder
) -> None:
    """Each step is placed by what cannot be undone. A scan after the push would be a scan
    of a published branch; a writer before it would risk the branch on a prose model."""
    await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.order == ["scan", "push", "begin:pr_writer", "open_pr"]


# ---- the push refusal ---------------------------------------------------------------------


async def test_a_committed_secret_stops_the_push_before_it_happens(
    tmp_path: Path, sent: Recorder
) -> None:
    """The refusal is asserted by what did *not* happen. A check that raised after the push
    would pass a test written the other way round."""
    sent.gitleaks = [leak()]

    with pytest.raises(RepoError, match="refusing to push"):
        await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.pushed == [], "nothing was pushed"
    assert sent.prs == [], "and no pull request was opened"
    assert sent.order == ["scan"], "it never got past the scan"


async def test_the_scan_is_fresh_rather_than_read_back_from_the_security_phase(
    tmp_path: Path, sent: Recorder
) -> None:
    """A fix round can introduce a secret *after* the scan that cleared the run, and
    `security_node` produces an empty report when there is no sandbox — while gitleaks runs
    on the worker and needs none. So the scan happens here, on the tree about to be pushed.
    """
    sent.gitleaks = [leak()]
    clean = SecurityReport(findings=[], critical=False, checklist={})

    with pytest.raises(RepoError, match="refusing to push"):
        await pr_node(state(security=clean), cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.pushed == [], "the phase said clean; the fresh scan disagreed and won"


async def test_a_secret_the_security_phase_found_is_not_forgotten_if_the_rescan_fails(
    tmp_path: Path, sent: Recorder
) -> None:
    """The two sources are folded together rather than one replacing the other. A rescan
    that cannot run yields `scan-failed`, which cannot gate — and a secret already found
    must not be cleared by that."""
    from tools.scanners import failure

    sent.gitleaks = [failure("gitleaks", "not on PATH")]
    found = SecurityReport(findings=[leak()], critical=True, checklist={})

    with pytest.raises(RepoError, match="refusing to push"):
        await pr_node(state(security=found), cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.pushed == []


async def test_the_refusal_names_the_file_and_never_the_value(
    tmp_path: Path, sent: Recorder
) -> None:
    """The message reaches a log, a step record and an API response. None of those is a
    place for the value."""
    secret = planted_secret("pr-node")
    sent.gitleaks = [leak(message=f"a key was committed (value withheld) {secret}")]

    with pytest.raises(RepoError) as caught:
        await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    assert "config.py:7" in str(caught.value), "a human has to know what to rotate"
    assert secret not in str(caught.value)
    assert "rotate" in str(caught.value)


async def test_the_refused_run_leaves_the_finding_on_the_record(
    tmp_path: Path, sent: Recorder
) -> None:
    """The run is about to end. The artifact is what somebody opens to find out what to
    rotate, and it is written before the exception rather than after it."""
    sent.gitleaks = [leak()]

    with pytest.raises(RepoError):
        await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    (kind, content) = sent.artifacts[0]
    assert kind == "gitleaks"
    assert content["findings"][0]["file"] == "config.py"
    assert "value withheld" in content["findings"][0]["message"]


async def test_a_scanner_that_could_not_run_does_not_block_the_push(
    tmp_path: Path, sent: Recorder
) -> None:
    """A scanner that did not run has found nothing. Refusing every push on a broken
    scanner would make the tool impossible to keep installed."""
    from tools.scanners import failure

    sent.gitleaks = [failure("gitleaks", "not on PATH")]

    await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.pushed == ["agent/x"]


# ---- the draft flag and the labels --------------------------------------------------------


async def test_a_clean_run_opens_a_normal_pull_request_with_both_labels(
    tmp_path: Path, sent: Recorder
) -> None:
    out = await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    (opened,) = sent.prs
    assert opened["draft"] is False
    assert opened["labels"] == ["autoswe", "needs-review"]
    assert opened["head"] == "agent/x" and opened["base"] == "main"
    assert out.pr_url == "https://github.com/a/b/pull/7"


async def test_a_run_with_unresolved_findings_opens_a_draft_that_names_them(
    tmp_path: Path, sent: Recorder
) -> None:
    """The whole point of Phase 4 arriving at a pull request: a run that gave up says so
    where a human will see it, rather than pushing quietly."""
    s = state(known_issues=["[blocking] src/a.py:9 — drops the last page"])

    await pr_node(s, cast("Any", FakeDeps()), resources(tmp_path))

    (opened,) = sent.prs
    assert opened["draft"] is True
    assert "drops the last page" in opened["body"]


async def test_the_event_says_whether_it_was_a_draft_and_who_wrote_it(
    tmp_path: Path, sent: Recorder
) -> None:
    """The console and the CLI read the stream, so "was this ready" has to be on it."""
    deps = FakeDeps()
    s = state(known_issues=["[blocking] x"])

    await pr_node(s, cast("Any", deps), resources(tmp_path))

    (_type, payload) = next(e for e in deps.events if e[0] == "pr_opened")
    assert payload["draft"] is True
    assert payload["known_issues"] == 1
    assert payload["written_by"] == "pr_writer"


# ---- the writer is replaceable ------------------------------------------------------------


async def test_a_writer_that_fails_does_not_cost_the_run_its_pull_request(
    tmp_path: Path, sent: Recorder
) -> None:
    """The work is done and pushed. Losing the pull request because a prose model was
    unavailable would be the wrong trade — and the body is still true without it."""
    deps = FakeDeps(FakeProvider(RuntimeError("the model is down")))

    out = await pr_node(state(), cast("Any", deps), resources(tmp_path))

    assert out.pr_url == "https://github.com/a/b/pull/7"
    (opened,) = sent.prs
    assert "did not run" in opened["body"], "and the body says it was generated"
    (_type, payload) = next(e for e in deps.events if e[0] == "pr_opened")
    assert payload["written_by"] == "fallback", "which is visible rather than silent"


async def test_the_pull_request_artifact_records_the_body_that_was_published(
    tmp_path: Path, sent: Recorder
) -> None:
    """ "What did the agent actually say about this change" should be answerable after the
    fact without reading a forge — and after somebody edits the description there."""
    await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    content = next(c for k, c in sent.artifacts if k == "pr")
    assert content["written_by"] == "pr_writer"
    assert content["draft"] is False
    assert content["description"]["title"] == "feat(ops): add subtract"
    assert content["body"] == sent.prs[0]["body"]


async def test_the_title_comes_from_the_writer_and_is_capped_by_the_harness(
    tmp_path: Path, sent: Recorder
) -> None:
    long = PullRequestDescription(
        title="feat(everything): " + "x" * 100,
        summary="s",
        changes=["c"],
        testing="t",
        known_issues=[],
        rollback="r",
    )
    deps = FakeDeps(FakeProvider(long))

    await pr_node(state(), cast("Any", deps), resources(tmp_path))

    from repo import pr_body

    assert len(sent.prs[0]["title"]) == pr_body.MAX_TITLE


# ---- the escalation path ------------------------------------------------------------------


async def _escalate(
    deps: Any, res: RunResources, *, reason: str = "debug_attempts_exhausted", **kw: Any
) -> RunState:
    """Take a run through `_fail`, which is the single point where it becomes FAILED."""
    import orchestrator.nodes as nodes

    s = state(phase=Phase.ESCALATE, escalation_reason=reason, **kw)
    return await nodes._fail(s, deps, res, "task t1 failed 3 times after a replan")


async def test_a_failed_run_with_commits_opens_a_draft_rather_than_losing_them(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that got three tasks in and died on the fourth has produced work somebody can
    finish. The alternative is a branch in a worktree that teardown deletes."""
    import orchestrator.nodes as nodes

    async def has_commits(*a: Any, **kw: Any) -> str:
        return "abc1234 feat: the part that worked"

    monkeypatch.setattr(nodes, "git", has_commits)
    deps = FakeDeps()

    out = await _escalate(deps, resources(tmp_path))

    assert out.resume_phase is Phase.FAILED, "it still failed"
    assert out.pr_url == "https://github.com/a/b/pull/7", "and the work is reachable"
    (opened,) = sent.prs
    assert opened["draft"] is True
    assert opened["title"].startswith("[WIP] "), "the title is where a list view reads it"
    assert "This run did not complete: debug_attempts_exhausted" in opened["body"]


async def test_a_failed_run_with_no_commits_opens_nothing(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pull request with no commits in it is a notification, not a contribution. The
    artifacts still carry what the run learned."""
    import orchestrator.nodes as nodes

    async def no_commits(*a: Any, **kw: Any) -> str:
        return "\n"

    monkeypatch.setattr(nodes, "git", no_commits)

    out = await _escalate(FakeDeps(), resources(tmp_path))

    assert out.pr_url is None
    assert sent.pushed == [] and sent.prs == []


async def test_a_secret_stops_the_draft_too(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escalation path goes through the same scan, because it is the same push. This is
    the reason it shares `_push_and_open` rather than carrying its own copy — the path less
    likely to be exercised is the one that must not be the one missing the gate."""
    import orchestrator.nodes as nodes

    async def has_commits(*a: Any, **kw: Any) -> str:
        return "abc1234 feat: with a key in it"

    monkeypatch.setattr(nodes, "git", has_commits)
    sent.gitleaks = [leak()]

    out = await _escalate(FakeDeps(), resources(tmp_path))

    assert sent.pushed == [] and sent.prs == []
    assert out.pr_url is None
    assert any(kind == "gitleaks" for kind, _ in sent.artifacts), "on the record to rotate"


async def test_a_pull_request_failure_never_masks_the_runs_own_failure(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`state.error` already holds the diagnosis. A GitHub outage must not replace "task t1
    failed 3 times" with "connection refused" — the first is why the run failed and the
    second is why a courtesy failed."""
    import orchestrator.nodes as nodes

    async def has_commits(*a: Any, **kw: Any) -> str:
        return "abc1234 feat: the part that worked"

    async def explode(*a: Any, **kw: Any) -> str:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(nodes, "git", has_commits)
    monkeypatch.setattr(nodes, "open_pr", explode)

    out = await _escalate(FakeDeps(), resources(tmp_path))

    assert out.resume_phase is Phase.FAILED
    assert out.error == "task t1 failed 3 times after a replan"
    assert "connection refused" not in (out.error or "")
    assert out.pr_url is None


async def test_no_github_client_is_a_logged_skip_rather_than_a_crash(
    tmp_path: Path, sent: Recorder
) -> None:
    """A run configured without a token should fail for its own reason, not for this one."""
    deps = FakeDeps()
    deps.github = None

    out = await _escalate(deps, resources(tmp_path))

    assert out.resume_phase is Phase.FAILED and out.pr_url is None


async def test_the_draft_carries_the_hypotheses_that_were_already_tried(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the draft. Somebody picking this up needs to know which theories have
    already been ruled out, and it is not visible anywhere else they would look."""
    import orchestrator.nodes as nodes
    from contracts import DebugHypothesis

    async def has_commits(*a: Any, **kw: Any) -> str:
        return "abc1234 feat: the part that worked"

    async def tried(deps: Any, run_id: Any, task_id: str) -> list[DebugHypothesis]:
        return [
            DebugHypothesis(
                failure_class="assertion",
                root_cause="the fixture returns a tuple, not a list",
                plan="convert at the boundary",
                confidence=0.6,
            )
        ]

    monkeypatch.setattr(nodes, "git", has_commits)
    monkeypatch.setattr(nodes, "_previous_hypotheses", tried)
    tasks = TaskGraph(tasks=[Task(spec=spec("t1"), status="failed")])

    await _escalate(FakeDeps(), resources(tmp_path), tasks=tasks)

    body = sent.prs[0]["body"]
    assert "## What was already tried" in body
    assert "the fixture returns a tuple, not a list" in body
    assert "convert at the boundary" in body


async def test_a_completed_run_carries_no_hypotheses_section(
    tmp_path: Path, sent: Recorder
) -> None:
    """On a run that succeeded these are noise, and the section would invite a reader to
    look for a problem that was solved."""
    await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    assert "## What was already tried" not in sent.prs[0]["body"]


# ---- a run with nothing committed ---------------------------------------------------------


async def test_a_run_that_committed_nothing_does_not_open_an_empty_pull_request(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by the first real-model run, which 808 scripted tests could not find.

    The model wrote working code, ran the tests in the sandbox — green, against the
    worktree — submitted its result, and never called `git_commit`. The run reached PR,
    pushed a branch identical to main, and opened a pull request containing nothing. It
    reported DONE.

    Every scripted fake in this suite calls `git_commit`, so nothing here had ever
    exercised the path where an agent simply forgets. A pull request with no commits in it
    is worse than no pull request: it reads as success and costs a reviewer the click.
    """
    import orchestrator.nodes as nodes

    async def no_commits(*a: Any, **kw: Any) -> str:
        return "\n"

    monkeypatch.setattr(nodes, "git", no_commits)

    with pytest.raises(RepoError, match="no commits"):
        await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    assert sent.pushed == [], "and nothing was pushed"
    assert sent.prs == []


async def test_the_empty_run_is_checked_before_the_branch_is_pushed(
    tmp_path: Path, sent: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order matters for the same reason the secret scan runs first: a branch pushed and
    then found to be empty has already left a dangling ref on the remote."""
    import orchestrator.nodes as nodes

    async def no_commits(*a: Any, **kw: Any) -> str:
        return ""

    monkeypatch.setattr(nodes, "git", no_commits)

    with pytest.raises(RepoError):
        await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))

    assert "push" not in sent.order, sent.order


# ---- cross-fork ---------------------------------------------------------------------------


async def test_the_run_s_upstream_reaches_the_pull_request(tmp_path: Path, sent: Recorder) -> None:
    """Where the PR is opened is the run's decision, taken when it was created.

    A node that dropped it would push to the fork and open the pull request on the fork
    too — which succeeds, returns a URL, and is silently the wrong repository. Nothing
    downstream could tell.
    """
    await pr_node(state(upstream="them/project"), cast("Any", FakeDeps()), resources(tmp_path))
    assert sent.prs[0]["upstream"] == "them/project"


async def test_an_ordinary_run_passes_no_upstream(tmp_path: Path, sent: Recorder) -> None:
    await pr_node(state(), cast("Any", FakeDeps()), resources(tmp_path))
    assert sent.prs[0]["upstream"] is None
