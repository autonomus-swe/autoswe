"""Run state and the full phase enum. Later phases add nodes, not enum values."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from contracts import Budget, RepoFacts, StateModel, TaskResult, TaskSpec, TestReport, Usage

DEFAULT_TEST_COMMAND = "uv run --no-sync pytest -q"


class Phase(StrEnum):
    SETUP = "setup"
    ANALYZE = "analyze"
    PLAN = "plan"
    DECOMPOSE = "decompose"
    CODE = "code"
    TEST = "test"
    DEBUG = "debug"
    REVIEW = "review"
    SECURITY = "security"
    PR = "pr"
    AWAITING_INPUT = "awaiting_input"
    ESCALATE = "escalate"
    DONE = "done"
    FAILED = "failed"


TERMINAL = frozenset({Phase.DONE, Phase.FAILED})

_STATUS = {Phase.DONE: "done", Phase.FAILED: "failed", Phase.AWAITING_INPUT: "awaiting_input"}


def status_for(phase: Phase) -> str:
    return _STATUS.get(phase, "running")


class RunState(StateModel):
    run_id: UUID
    goal: str
    repo_url: str
    base_branch: str
    work_branch: str
    phase: Phase = Phase.SETUP
    base_sha: str | None = None
    facts: RepoFacts | None = None
    test_command: str = DEFAULT_TEST_COMMAND
    task: TaskSpec | None = None  # v1: one synthetic task built from the goal
    task_result: TaskResult | None = None
    last_test_report: TestReport | None = None
    pr_url: str | None = None
    error: str | None = None
    pushed: bool = False
    budget: Budget = Budget()
    usage: Usage = Usage()
