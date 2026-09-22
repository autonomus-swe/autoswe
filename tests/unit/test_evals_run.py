"""The eval driver, against a control plane and a verifier that are both scripted.

What is under test is the bookkeeping: that a row says what happened, that a run which
never finished is not quietly counted as resolved, and that a task with nothing to verify
is reported as unverifiable rather than folded into either column. Those are the ways a
harness lies about the system it measures, and they are silent.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest

from evals import run as evals_run
from evals.suite import Suite, Task, Verify

pytestmark = pytest.mark.unit


def task(**over: Any) -> Task:
    base: dict[str, Any] = {
        "id": "demo",
        "repo": "https://github.com/acme/fixture",
        "goal": "Implement subtract(a, b) so the tests pass.",
        "verify": Verify(command="pytest -q"),
        "timeout_s": 5.0,
        "budget_usd": 3.0,
    }
    return Task(**{**base, **over})


DETAIL = {
    "run": {"run_id": "r1", "status": "done", "pr_url": "https://x/1", "error": None},
    "tasks": [{"id": "t1"}, {"id": "t2"}],
    "steps": [
        {"agent": "analyzer"},
        {"agent": "coder"},
        {"agent": "debugger"},
        {"agent": "debugger"},
        {"agent": "review"},
        {"agent": "review_pre"},
    ],
    "totals": {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 3000,
        "cost_usd": 0.25,
    },
}


class FakeClient:
    """The four calls the harness makes, scripted. Records what it was asked for."""

    def __init__(self, statuses: list[str] | None = None, detail: dict[str, Any] | None = None):
        self.statuses = statuses or ["done"]
        self.detail_body = detail if detail is not None else DETAIL
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self._polls = 0

    async def create(self, t: Task, provider: str | None, ablation: str | None = None) -> str:
        self.created.append(
            {"task": t.id, "provider": provider, "budget": t.budget_usd, "ablation": ablation}
        )
        return "r1"

    async def summary(self, run_id: str) -> dict[str, Any]:
        status = self.statuses[min(self._polls, len(self.statuses) - 1)]
        self._polls += 1
        return {"run_id": run_id, "status": status, "work_branch": "agent/r1"}

    async def detail(self, run_id: str) -> dict[str, Any]:
        return self.detail_body

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def aclose(self) -> None:
        return None


def verifier(exit_code: int = 0, output: str = "ok") -> Any:
    async def verify(t: Task, summary: dict[str, Any]) -> tuple[int, str]:
        return exit_code, output

    return verify


async def test_a_resolved_task_records_what_the_run_cost_and_how_hard_it_was() -> None:
    client = FakeClient()
    row = await evals_run.run_task(client, task(), verifier=verifier(0))

    assert row.resolved is True and row.status == "done"
    assert row.tasks == 2
    assert row.debug_attempts == 2
    assert row.review_rounds == 2  # `review` and `review_pre` are both review steps
    assert row.cost_usd == 0.25
    assert row.pr_url == "https://x/1"
    assert row.cache_hit_rate == 0.75  # 3000 / (1000 + 3000)


async def test_a_failing_verify_command_is_not_resolved() -> None:
    client = FakeClient()
    row = await evals_run.run_task(client, task(), verifier=verifier(1, "2 failed"))
    assert row.resolved is False and row.verify_exit == 1
    assert "2 failed" in row.verify_output


async def test_a_task_expecting_a_non_zero_exit_is_resolved_by_it() -> None:
    """Some tasks are "this must keep failing". The expectation is the task's, not a
    hard-coded zero."""
    client = FakeClient()
    row = await evals_run.run_task(
        client, task(verify=Verify(command="pytest -q", expect_exit=1)), verifier=verifier(1)
    )
    assert row.resolved is True


async def test_a_run_that_failed_is_not_verified_at_all() -> None:
    """Cloning a branch that may not exist would report a git error as a test failure, and
    the row would blame the task for the run's problem."""
    called = False

    async def never(t: Task, summary: dict[str, Any]) -> tuple[int, str]:
        nonlocal called
        called = True
        return 0, ""

    client = FakeClient(statuses=["failed"], detail={**DETAIL, "run": {"status": "failed"}})
    row = await evals_run.run_task(client, task(), verifier=never)
    assert row.resolved is False and not called


async def test_a_task_with_nothing_to_verify_is_neither_resolved_nor_unresolved() -> None:
    """`None`, so the report counts it in its own column. A harness that called it `True`
    would report a hundred per cent the moment somebody forgot a verify block."""
    client = FakeClient()
    row = await evals_run.run_task(client, task(verify=None), verifier=verifier(0))
    assert row.resolved is None


async def test_a_run_that_outlives_its_timeout_is_cancelled_not_abandoned() -> None:
    """It holds a worker slot, a container and a worktree. A suite of thirty that leaks
    one per task does not finish."""
    client = FakeClient(statuses=["running"])
    row = await evals_run.run_task(client, task(timeout_s=0.01), verifier=verifier(0))
    assert row.status == "timeout" and row.resolved is False
    assert client.cancelled == ["r1"]


async def test_a_crash_in_the_harness_becomes_a_row() -> None:
    """The outcome most worth having in the record is the one nobody wants to write down."""

    class Broken(FakeClient):
        async def create(self, t: Task, provider: str | None, ablation: str | None = None) -> str:
            raise RuntimeError("the control plane refused")

    row = await evals_run.run_task(Broken(), task(), verifier=verifier(0))
    assert row.status == "error" and row.resolved is False
    assert row.error is not None and "refused" in row.error


async def test_the_request_the_harness_actually_posts() -> None:
    """The real `Client`, against a transport that captures the body.

    Worth doing against the real one rather than the fake: `unattended` is the field that
    matters and it is set in `Client.create`, so a fake that stands in for that method
    would assert its own behaviour. Nobody is watching a suite of thirty — an attended run
    parks on its first approval and burns the whole timeout waiting for somebody asleep.
    """
    import httpx

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1"})

    client = evals_run.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        assert await client.create(task(), "openai_compat") == "r1"
    finally:
        await client.aclose()

    assert captured["unattended"] is True
    assert captured["budget"] == {"max_usd": 3.0}
    assert captured["provider"] == "openai_compat"
    assert captured["repo_url"] == "https://github.com/acme/fixture"


async def test_no_provider_means_the_field_is_absent_not_empty() -> None:
    """`RunCreate.provider` is validated against what the build can make, and the empty
    string is not one of them — sending it would turn "use the default" into a 422."""
    import httpx

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1"})

    client = evals_run.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        await client.create(task(), None)
    finally:
        await client.aclose()
    assert "provider" not in captured


async def test_the_suite_writes_a_row_per_task_as_it_goes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Appended as each finishes rather than at the end: a suite is an hour of real runs,
    and a harness that loses all of it to a crash on the last task is one nobody runs
    twice."""
    monkeypatch.setenv("AUTOSWE_EVAL_RESULTS", str(tmp_path))
    monkeypatch.setattr(evals_run, "Client", lambda *a, **k: FakeClient())

    suite = Suite(name="private", tasks=(task(id="a"), task(id="b")))
    rows = await evals_run.run_suite(
        suite, api="http://x", key="k", verifier=verifier(0), results_name="t"
    )
    assert {r.task_id for r in rows} == {"a", "b"}

    from evals import record

    written = record.read("t")
    assert {r["task_id"] for r in written} == {"a", "b"}
    assert all(r["resolved"] is True for r in written)


def test_measure_reads_counts_from_steps_rather_than_a_summary_field() -> None:
    """Six Debugger steps is six attempts, whatever any phase log says about it."""
    row = evals_run.measure(DETAIL)
    assert row.debug_attempts == 2 and row.review_rounds == 2 and row.tasks == 2
    assert asdict(row)["cache_hit_rate"] == 0.75


def test_measure_survives_a_detail_with_nothing_in_it() -> None:
    """A run that failed in SETUP has no steps, no tasks and no totals. The row should say
    zero rather than raise, so the failure is recorded instead of hiding the whole task."""
    row = evals_run.measure({})
    assert row.tasks == 0 and row.cost_usd == 0.0 and row.cache_hit_rate == 0.0


# ---- ablation arms -------------------------------------------------------------------------


async def test_an_arm_changes_the_request_and_is_recorded_on_the_row() -> None:
    """Recorded rather than inferred: what an ablation changed is a fact about how the run
    was made, and `report.compare` groups by that fact instead of guessing from the shape
    of the results."""
    client = FakeClient()
    row = await evals_run.run_task(client, task(), verifier=verifier(0), ablation="no-debugger")
    assert row.ablation == "no-debugger"
    assert client.created[0]["ablation"] == "no-debugger"


async def test_the_no_debugger_arm_sends_a_zero_ceiling() -> None:
    """`Budget.max_debug_attempts = 0` sends the first failing test straight to ESCALATE,
    which is what "without the Debugger" means for this state machine."""
    import httpx

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1"})

    client = evals_run.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        await client.create(task(), None, "no-debugger")
    finally:
        await client.aclose()
    assert captured["budget"]["max_debug_attempts"] == 0


async def test_an_arm_merges_into_the_budget_rather_than_replacing_it() -> None:
    """The task's dollar ceiling has to survive. An arm that reset the whole budget would
    be changing two things and reporting one."""
    import httpx

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1"})

    client = evals_run.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        await client.create(task(), None, "no-debugger")
    finally:
        await client.aclose()
    assert captured["budget"] == {"max_usd": 3.0, "max_debug_attempts": 0}


async def test_no_arm_leaves_the_request_alone() -> None:
    import httpx

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1"})

    client = evals_run.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        await client.create(task(), None, None)
    finally:
        await client.aclose()
    assert captured["budget"] == {"max_usd": 3.0}


def test_only_arms_that_are_actually_per_run_are_offered() -> None:
    """`no-repomap` is a worker process setting (`REPO_MAP_VERSION=v1`), not a run field.
    Offering it here and quietly ignoring it would produce two identical columns with
    different labels, which is worse than not offering it at all."""
    assert set(evals_run.ABLATIONS) == {"no-debugger"}


async def test_a_task_pins_its_base_commit_when_it_has_one() -> None:
    """A benchmark instance does; an ordinary task does not, and must not send a null that
    the schema would then have to accept."""
    import httpx

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1"})

    client = evals_run.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        await client.create(task(base_commit="a" * 40), None)
        assert captured["base_commit"] == "a" * 40
        captured.clear()
        await client.create(task(), None)
        assert "base_commit" not in captured
    finally:
        await client.aclose()
