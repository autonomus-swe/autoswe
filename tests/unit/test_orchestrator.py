"""Transition table, node behaviour with fakes, and teardown guarantees."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from contracts import (
    ImplementationPlan,
    RepoProfile,
    TaskGraph,
    TaskGraphSpec,
    TaskResult,
    TaskSpec,
    TestReport,
)
from orchestrator.nodes import (
    HARNESS_PACKAGES,
    RunResources,
    install_command,
    synthetic_task,
    teardown,
)
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


PLAN_OK = ImplementationPlan(
    approach="a",
    affected_files=[],
    new_files=[],
    risks=[],
    test_strategy="pytest",
    open_questions=[],
)
PLAN_ASKS = PLAN_OK.model_copy(update={"open_questions": ["cookies or JWT?"]})
PROFILE = RepoProfile(
    languages=["python"],
    framework=None,
    package_manager="uv",
    test_command="pytest -q",
    lint_command=None,
    conventions=[],
    entry_points=[],
)


def spec(tid: str, depends: list[str] | None = None) -> TaskSpec:
    return TaskSpec(
        id=tid,
        title=tid,
        description="d",
        depends_on=depends or [],
        files=[],
        acceptance_criteria=["ok"],
        test_selector="tests/test_x.py",
    )


def graph(*ids: str) -> TaskGraph:
    return TaskGraph.from_spec(TaskGraphSpec(tasks=[spec(i) for i in ids]))


@pytest.mark.parametrize(
    ("current", "kw", "expected"),
    [
        (Phase.SETUP, {}, Phase.ANALYZE),
        (Phase.ANALYZE, {"repo": PROFILE}, Phase.PLAN),
        (Phase.ANALYZE, {}, Phase.FAILED),
        (Phase.PLAN, {"plan": PLAN_OK}, Phase.DECOMPOSE),
        (Phase.PLAN, {"plan": PLAN_ASKS}, Phase.AWAITING_INPUT),
        (Phase.PLAN, {}, Phase.FAILED),
        (Phase.AWAITING_INPUT, {}, Phase.PLAN),
        (Phase.DECOMPOSE, {"tasks": graph("t1")}, Phase.CODE),
        (Phase.DECOMPOSE, {}, Phase.FAILED),
        (
            Phase.CODE,
            {"tasks": graph("t1"), "current_task_id": "t1", "task_results": {"t1": RESULT}},
            Phase.TEST,
        ),
        # v3: a Coder that produced nothing is a failed attempt, not a dead run
        (Phase.CODE, {"tasks": graph("t1"), "current_task_id": "t1"}, Phase.ESCALATE),
        # v3: a failing test is what DEBUG exists for
        (Phase.TEST, {"last_test_report": report(False)}, Phase.DEBUG),
        (Phase.TEST, {}, Phase.ESCALATE),
        (Phase.DEBUG, {}, Phase.TEST),
        (Phase.PR, {"pr_url": "https://example/pull/1"}, Phase.DONE),
        (Phase.PR, {}, Phase.FAILED),
    ],
)
def test_transition_table(current: Phase, kw: dict[str, Any], expected: Phase) -> None:
    assert transition(state(phase=current, **kw)) == expected


def test_test_phase_returns_to_code_while_tasks_remain_then_goes_to_review() -> None:
    """The multi-task loop: CODE and TEST alternate until the graph is exhausted, and the
    finished change is reviewed before it is pushed."""
    tasks = graph("t1", "t2", "t3")
    s = state(phase=Phase.TEST, tasks=tasks, last_test_report=report(True))

    for task_id in ("t1", "t2"):
        tasks.by_id(task_id).status = "done"
        assert transition(s) == Phase.CODE, f"after {task_id} there is still work"

    tasks.by_id("t3").status = "done"
    assert transition(s) == Phase.REVIEW


def test_transition_rejects_phases_without_a_node() -> None:
    """SECURITY is declared and not yet routed. tests/unit/test_transition.py keeps the
    authoritative list; this is the smoke check that the guard exists at all."""
    with pytest.raises(ValueError, match="no transition"):
        transition(state(phase=Phase.SECURITY))


def test_status_mapping() -> None:
    assert status_for(Phase.DONE) == "done" and status_for(Phase.FAILED) == "failed"
    assert status_for(Phase.CODE) == "running"
    assert Phase.DONE in {Phase.DONE, Phase.FAILED}


def test_install_command_prefers_the_detected_facts(tmp_path: Path) -> None:
    """SETUP must use what detection found, not re-derive it from file existence."""
    from contracts import RepoFacts

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    facts = RepoFacts(install_command="poetry install", package_manager="poetry")
    cmd = install_command(tmp_path, facts)
    assert cmd.startswith("(poetry install)")
    assert cmd.endswith("uv pip install " + HARNESS_PACKAGES)

    # no facts, or facts without a command, falls back to inspecting the tree
    assert install_command(tmp_path, RepoFacts()).startswith("(uv sync")
    assert install_command(tmp_path, None).startswith("(uv sync")


def test_install_command_matches_the_manifest_and_always_adds_the_harness(
    tmp_path: Path,
) -> None:
    # run_tests needs pytest-json-report inside the project venv, whatever the repo uses
    assert install_command(tmp_path) == "uv venv && uv pip install " + HARNESS_PACKAGES
    (tmp_path / "requirements.txt").write_text("pytest\n")
    with_reqs = install_command(tmp_path)
    assert with_reqs.startswith("uv venv && uv pip install -r requirements.txt")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    with_project = install_command(tmp_path)
    assert with_project.startswith("(uv sync --all-extras || uv sync)")
    for cmd in (with_reqs, with_project):
        assert cmd.endswith("uv pip install " + HARNESS_PACKAGES)


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


async def test_worker_refuses_to_start_without_its_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worker missing GITHUB_TOKEN must fail at startup, not halfway through a run."""
    from core.settings import EXIT_CONFIG, get_settings
    from orchestrator.worker import configure_worker

    for name, value in (
        ("DATABASE_URL", "postgresql+asyncpg://u@h/d"),
        ("REDIS_URL", "redis://h:6379/0"),
        ("API_KEYS", "unit-test-key"),
        ("LLM_API_KEY", "sk-or-unit-test"),
        ("GITHUB_TOKEN", ""),
    ):
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            await configure_worker({})
        assert exc.value.code == EXIT_CONFIG
        assert "GITHUB_TOKEN" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()


def test_ca_bundle_is_applied_to_requests_and_ssl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behind a TLS-inspecting proxy, requests and the stdlib must trust the OS CAs."""
    import os

    from core.errors import ConfigError
    from core.settings import load_settings
    from orchestrator.worker import apply_ca_bundle

    bundle = tmp_path / "ca-certificates.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    for name, value in (
        ("DATABASE_URL", "postgresql+asyncpg://u@h/d"),
        ("REDIS_URL", "redis://h:6379/0"),
        ("API_KEYS", "unit-test-key"),
        ("CA_BUNDLE", str(bundle)),
    ):
        monkeypatch.setenv(name, value)

    settings = load_settings(env_file=None)
    apply_ca_bundle(settings)
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle)
    assert os.environ["SSL_CERT_FILE"] == str(bundle)
    assert settings.public_dict()["ca_bundle"] == str(bundle)

    # an operator's own export is never overridden
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/operator/choice.pem")
    apply_ca_bundle(settings)
    assert os.environ["REQUESTS_CA_BUNDLE"] == "/operator/choice.pem"

    # a path that does not exist is a configuration error, not a silent no-op
    monkeypatch.setenv("CA_BUNDLE", str(tmp_path / "missing.crt"))
    with pytest.raises(ConfigError, match="does not exist"):
        apply_ca_bundle(load_settings(env_file=None))


def test_no_ca_bundle_leaves_the_environment_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from core.settings import load_settings
    from orchestrator.worker import apply_ca_bundle

    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CA_BUNDLE", raising=False)
    for name, value in (
        ("DATABASE_URL", "postgresql+asyncpg://u@h/d"),
        ("REDIS_URL", "redis://h:6379/0"),
        ("API_KEYS", "unit-test-key"),
    ):
        monkeypatch.setenv(name, value)
    settings = load_settings(env_file=None)
    apply_ca_bundle(settings)
    assert "REQUESTS_CA_BUNDLE" not in os.environ
    assert settings.public_dict()["ca_bundle"] == "certifi default"


def test_worker_module_imports_without_any_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the worker must not require a configured environment.

    WorkerSettings used to evaluate get_settings() at class-definition time, so the
    import itself exited with code 2 wherever no .env existed — CI, a fresh clone, or
    any tool that only wants to read the module.
    """
    import importlib

    from core.settings import get_settings

    for var in ("DATABASE_URL", "REDIS_URL", "API_KEYS"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    try:
        module = importlib.reload(importlib.import_module("orchestrator.worker"))
        assert [f.__name__ for f in module.WorkerSettings.functions] == ["run_job"]
        assert module.WorkerSettings.on_startup.__name__ == "configure_worker"
        # arq reads this straight out of the class dict, so it must be a real value
        from arq.connections import RedisSettings

        assert isinstance(vars(module.WorkerSettings)["redis_settings"], RedisSettings)
    finally:
        get_settings.cache_clear()
