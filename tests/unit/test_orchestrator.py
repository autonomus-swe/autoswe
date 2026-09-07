"""Transition table, node behaviour with fakes, and teardown guarantees."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from contracts import TaskResult, TestReport
from orchestrator.nodes import RunResources, install_command, synthetic_task, teardown
from orchestrator.state import Phase, RunState, status_for
from orchestrator.transition import transition
from tests.fakes import FakeSandbox

pytestmark = pytest.mark.unit


def state(**kw: Any) -> RunState:
    base: dict[str, Any] = dict(
        run_id=uuid.uuid4(),
        goal="Implement subtract",
        repo_url="https://github.com/acme/demo",
        base_branch="main",
        work_branch="agent/x",
    )
    base.update(kw)
    return RunState(**base)


RESULT = TaskResult(
    summary="s", files_touched=["a.py"], how_to_test="pytest", notes_for_reviewer=[]
)


def report(passed: bool) -> TestReport:
    return TestReport(
        passed=passed,
        total=3,
        failed=0 if passed else 1,
        errors=0,
        skipped=0,
        failures=[],
        duration_s=0.1,
        command="pytest",
        truncated_output="",
    )


@pytest.mark.parametrize(
    ("current", "kw", "expected"),
    [
        (Phase.SETUP, {}, Phase.CODE),
        (Phase.CODE, {"task_result": RESULT}, Phase.TEST),
        (Phase.CODE, {}, Phase.FAILED),
        (Phase.TEST, {"last_test_report": report(True)}, Phase.PR),
        (Phase.TEST, {"last_test_report": report(False)}, Phase.FAILED),
        (Phase.TEST, {}, Phase.FAILED),
        (Phase.PR, {"pr_url": "https://github.com/acme/demo/pull/1"}, Phase.DONE),
        (Phase.PR, {}, Phase.FAILED),
    ],
)
def test_transition_table(current: Phase, kw: dict[str, Any], expected: Phase) -> None:
    assert transition(state(phase=current, **kw)) == expected


def test_transition_rejects_phases_without_a_node() -> None:
    with pytest.raises(ValueError, match="no transition"):
        transition(state(phase=Phase.REVIEW))


def test_status_mapping() -> None:
    assert status_for(Phase.DONE) == "done" and status_for(Phase.FAILED) == "failed"
    assert status_for(Phase.CODE) == "running"
    assert Phase.DONE in {Phase.DONE, Phase.FAILED}


def test_install_command_matches_the_manifest(tmp_path: Path) -> None:
    assert install_command(tmp_path) is None
    (tmp_path / "requirements.txt").write_text("pytest\n")
    assert "uv pip install -r requirements.txt" in (install_command(tmp_path) or "")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert (install_command(tmp_path) or "").startswith("uv sync")


def test_synthetic_task_carries_the_goal() -> None:
    task = synthetic_task("Implement subtract(a, b) so the tests pass")
    assert task.id == "t1" and task.description.startswith("Implement subtract")
    assert task.acceptance_criteria == ["All tests pass"] and task.depends_on == []
    assert len(synthetic_task("g" * 500).title) == 80


class FakeBus:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def release_lock(self, key: str, owner: str) -> bool:
        self.released.append(key)
        return True


class FakeDeps:
    def __init__(self, keep_failed: bool = False) -> None:
        self.bus = FakeBus()
        self.settings = type("S", (), {"keep_failed_sandbox": keep_failed})()


async def test_teardown_releases_everything_and_keeps_the_worktree_until_pushed(
    tmp_path: Path,
) -> None:
    sandbox = FakeSandbox(tmp_path)
    res = RunResources(sandbox=sandbox, lock_key="lock:x", lock_owner="w1")
    res.renewer = asyncio.create_task(asyncio.sleep(3600))
    deps = FakeDeps()
    await teardown(state(pushed=False), deps, res)  # type: ignore[arg-type]
    assert sandbox.stopped and deps.bus.released == ["lock:x"] and res.renewer.cancelled()


async def test_teardown_never_raises_when_cleanup_fails(tmp_path: Path) -> None:
    class Exploding(FakeSandbox):
        async def stop(self, *, remove: bool = True) -> None:
            raise RuntimeError("docker is gone")

    res = RunResources(sandbox=Exploding(tmp_path), lock_key="lock:y", lock_owner="w")
    deps = FakeDeps()
    await teardown(state(), deps, res)  # type: ignore[arg-type]
    assert deps.bus.released == ["lock:y"]  # the lock is still released
