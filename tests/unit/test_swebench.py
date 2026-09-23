"""Producing SWE-bench predictions.

The benchmark scores patches. It does not care whether the run that produced one went on
to open a pull request, and this file exists mostly to keep that straight — because the
first version of the producer gated the patch on `status == "done"` and threw away every
patch from a run that died after TEST.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import swebench

pytestmark = pytest.mark.unit

INSTANCE = swebench.Instance(
    instance_id="django__django-11099",
    repo="django/django",
    base_commit="d26b2424437dabeeca94d7900b37d2df4410da0c",
    problem_statement="UsernameValidator allows trailing newline in usernames.",
)


def client_returning(status: int, body: str) -> swebench.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    client = swebench.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    return client


# ---- the patch --------------------------------------------------------------------------


async def test_the_patch_is_taken_whatever_the_run_did_afterwards() -> None:
    """The `diff` artifact is written in TEST, before REVIEW, SECURITY and PR.

    A run that died in the security scan — or at the push, which is what happens against a
    repository you do not own — still holds the patch it produced. Gating on `done` threw
    those away and made the benchmark look as though it needed a writable fork of every
    upstream repository.
    """
    client = client_returning(200, "--- a/django/x.py\n+++ b/django/x.py\n+fixed\n")
    try:
        assert "+fixed" in await swebench.patch_for(client, "r1")
    finally:
        await client.aclose()


async def test_a_run_with_no_diff_gives_an_empty_patch() -> None:
    """Which is a legitimate prediction meaning "no patch was produced" — not a row to
    omit, because omitting it shrinks the denominator."""
    client = client_returning(404, "no diff artifact")
    try:
        assert await swebench.patch_for(client, "r1") == ""
    finally:
        await client.aclose()


async def test_a_control_plane_that_is_unreachable_does_not_stop_the_batch() -> None:
    """Fifty instances is hours. One unreachable read must not lose the other forty-nine."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = swebench.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        assert await swebench.patch_for(client, "r1") == ""
    finally:
        await client.aclose()


# ---- the instance -------------------------------------------------------------------------


def test_an_instance_becomes_a_task_pinned_to_its_commit() -> None:
    """The commit is the whole reason `RunCreate.base_commit` exists: a patch produced
    against a branch head does not apply to the instance's base."""
    task = INSTANCE.task(fork_owner=None, budget_usd=3.0, timeout_s=900.0)
    assert task.id == "django__django-11099"
    assert task.base_commit == INSTANCE.base_commit
    assert task.repo == "https://github.com/django/django"
    assert INSTANCE.problem_statement.startswith(task.goal[:20])


def test_a_fork_owner_redirects_the_clone_and_nothing_else() -> None:
    """For pushing. The instance id, the goal and the commit are the benchmark's."""
    task = INSTANCE.task(fork_owner="me", budget_usd=3.0, timeout_s=900.0)
    assert task.repo == "https://github.com/me/django"
    assert task.base_commit == INSTANCE.base_commit


def test_a_task_has_no_verify_command() -> None:
    """Scoring is the official harness's job. A verify command here would be this project
    marking its own benchmark homework."""
    assert INSTANCE.task(None, 3.0, 900.0).verify is None


# ---- the file the harness reads -------------------------------------------------------------


def test_a_prediction_carries_the_three_fields_the_harness_reads() -> None:
    row = swebench._prediction(INSTANCE, "PATCH", {"status": "failed"})
    assert row["instance_id"] == "django__django-11099"
    assert row["model_name_or_path"] == swebench.MODEL_NAME
    assert row["model_patch"] == "PATCH"


def test_our_own_fields_travel_under_one_key() -> None:
    """The harness ignores what it does not know, and cost per resolved instance cannot be
    reconstructed later from a file that only carries patches."""
    row = swebench._prediction(INSTANCE, "PATCH", {"status": "failed", "cost_usd": 0.5})
    assert row["autoswe"] == {"status": "failed", "cost_usd": 0.5}
    assert set(row) == {"instance_id", "model_name_or_path", "model_patch", "autoswe"}


def test_the_status_travels_so_a_reader_can_tell_the_two_failures_apart(tmp_path: Path) -> None:
    """ "Solved it and could not push" and "produced nothing" both end `failed`, and only
    the patch tells them apart. Both are in the file."""
    rows: list[dict[str, Any]] = [
        swebench._prediction(INSTANCE, "PATCH", {"status": "failed"}),
        swebench._prediction(INSTANCE, "", {"status": "failed"}),
    ]
    path = swebench.write(rows, tmp_path / "predictions.jsonl")
    written = [json.loads(line) for line in path.read_text().splitlines()]
    assert [bool(r["model_patch"]) for r in written] == [True, False]
    assert all(r["autoswe"]["status"] == "failed" for r in written)


def test_predictions_are_one_json_object_per_line(tmp_path: Path) -> None:
    """The format the harness reads. A pretty-printed file is not JSONL."""
    path = swebench.write([swebench._prediction(INSTANCE, "P", {})], tmp_path / "p.jsonl")
    lines = path.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["model_patch"] == "P"


def test_the_dataset_is_the_lite_split() -> None:
    """Quoting a number against the wrong split is worse than quoting none."""
    assert swebench.DATASET == "princeton-nlp/SWE-bench_Lite"


def test_datasets_absence_is_an_instruction_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is deliberately not a dependency — pyarrow and several hundred megabytes for one
    optional benchmark."""
    import builtins

    real = builtins.__import__

    def no_datasets(name: str, *a: Any, **k: Any) -> Any:
        if name == "datasets":
            raise ImportError("no module named datasets")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_datasets)
    with pytest.raises(SystemExit, match="uv pip install datasets"):
        swebench.load_instances(5)


# ---- the whole producer ----------------------------------------------------------------


class FakePlane:
    """A control plane whose runs end `failed` but produced a patch.

    The exact case the `done` gate discarded, and the ordinary one for this benchmark: a
    run against `django/django` cannot push, so it fails at PR with a perfectly good diff
    already saved from TEST.
    """

    def __init__(self, status: str = "failed", diff: str = "--- a/x\n+++ b/x\n+fixed\n") -> None:
        self.status = status
        self.diff = diff
        self.created: list[Any] = []
        self._client = self  # `patch_for` reaches through this

    async def create(self, task: Any, provider: str | None, ablation: str | None = None) -> str:
        self.created.append(task)
        return "r1"

    async def summary(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "status": self.status}

    async def detail(self, run_id: str) -> dict[str, Any]:
        return {"run": {"run_id": run_id, "status": self.status}, "steps": [], "totals": {}}

    async def cancel(self, run_id: str) -> None:
        return None

    async def get(self, url: str) -> Any:
        status = 200 if self.diff else 404
        return httpx.Response(status, text=self.diff, request=httpx.Request("GET", "http://x"))

    async def aclose(self) -> None:
        return None


async def test_a_failed_run_that_produced_a_patch_still_predicts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutation that survived the first version of this file.

    `patch_for` was tested in isolation, so re-introducing `if status == "done"` at the
    call site left the suite green — the same shape that dropped `upstream` from the PR
    node and the ablation arm from the CLI.
    """
    plane = FakePlane(status="failed")
    monkeypatch.setattr(swebench, "Client", lambda *a, **k: plane)

    [prediction] = await swebench.predict(
        [INSTANCE], api="http://x", key="k", fork_owner=None, timeout_s=1.0
    )

    assert "+fixed" in prediction["model_patch"], "a failed push is not an unsolved instance"
    assert prediction["autoswe"]["status"] == "failed"
    assert prediction["instance_id"] == INSTANCE.instance_id


async def test_a_run_that_produced_nothing_predicts_an_empty_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane = FakePlane(status="failed", diff="")
    monkeypatch.setattr(swebench, "Client", lambda *a, **k: plane)

    [prediction] = await swebench.predict(
        [INSTANCE], api="http://x", key="k", fork_owner=None, timeout_s=1.0
    )
    assert prediction["model_patch"] == ""
    assert prediction["autoswe"]["status"] == "failed"


async def test_an_instance_whose_run_crashes_is_still_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fifty instances is hours and real money. One that raises must not lose the rest,
    and must not silently shrink the denominator either."""

    class Broken(FakePlane):
        async def create(self, task: Any, provider: str | None, ablation: str | None = None) -> str:
            raise RuntimeError("the control plane refused")

    monkeypatch.setattr(swebench, "Client", lambda *a, **k: Broken())
    [prediction] = await swebench.predict(
        [INSTANCE], api="http://x", key="k", fork_owner=None, timeout_s=1.0
    )
    assert prediction["model_patch"] == ""
    assert "refused" in prediction["autoswe"]["error"]


class FakeClock:
    """Readings handed out in order, so a duration is an exact number rather than a range.

    Substituted for the module's `time` — not for `time.monotonic` globally, which is the
    clock asyncio schedules on.
    """

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)
        self.calls = 0

    def monotonic(self) -> float:
        reading = self._readings[min(self.calls, len(self._readings) - 1)]
        self.calls += 1
        return reading


async def test_a_prediction_records_how_long_the_instance_took(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`measure` never sets `wall_clock_s` — `run_task` does, and this path does not go
    through it. Every prediction said `0.0` while the first real instance attempted took
    fifty-two minutes, which is the number you need to decide whether fifty is affordable.

    Asserted as an exact duration. The first version of this test asserted `>= 0.0`, which
    is true of `0.0` — it passed against the bug it was written for, and the mutation that
    restored the bug survived it.
    """
    monkeypatch.setattr(swebench, "time", FakeClock(1000.0, 4151.0))
    monkeypatch.setattr(swebench, "Client", lambda *a, **k: FakePlane(status="done"))
    [prediction] = await swebench.predict(
        [INSTANCE], api="http://x", key="k", fork_owner=None, timeout_s=1.0
    )
    assert prediction["autoswe"]["wall_clock_s"] == 3151.0


async def test_an_instance_that_crashed_still_records_its_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The minutes were spent whether or not the run finished, and a batch's cost is the
    sum of what it spent rather than of what it produced."""

    class Broken(FakePlane):
        async def create(self, task: Any, provider: str | None, ablation: str | None = None) -> str:
            raise RuntimeError("refused")

    monkeypatch.setattr(swebench, "time", FakeClock(1000.0, 1042.5))
    monkeypatch.setattr(swebench, "Client", lambda *a, **k: Broken())
    [prediction] = await swebench.predict(
        [INSTANCE], api="http://x", key="k", fork_owner=None, timeout_s=1.0
    )
    assert prediction["autoswe"]["wall_clock_s"] == 42.5
    assert "refused" in prediction["autoswe"]["error"]
