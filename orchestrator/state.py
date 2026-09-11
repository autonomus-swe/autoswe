"""Run state and the full phase enum. Later phases add nodes, not enum values."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from contracts import (
    Budget,
    ImplementationPlan,
    RepoFacts,
    RepoProfile,
    ReviewReport,
    SecurityReport,
    StateModel,
    TaskGraph,
    TaskResult,
    TaskSpec,
    TestReport,
    Usage,
)

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
    """Everything a run needs to continue from a cold start.

    This is the checkpoint payload, so every field must survive
    ``model_dump(mode="json")`` then ``model_validate`` unchanged.
    """

    run_id: UUID
    goal: str
    repo_url: str
    base_branch: str
    work_branch: str
    phase: Phase = Phase.SETUP
    base_sha: str | None = None
    unattended: bool = False
    test_command: str = DEFAULT_TEST_COMMAND

    facts: RepoFacts | None = None
    repo: RepoProfile | None = None
    plan: ImplementationPlan | None = None
    answers: list[tuple[str, str]] = Field(default_factory=list)
    tasks: TaskGraph | None = None
    current_task_id: str | None = None
    attempts: dict[str, int] = Field(default_factory=dict)
    task_results: dict[str, TaskResult] = Field(default_factory=dict)
    last_test_report: TestReport | None = None

    review: ReviewReport | None = None  # Phase 4
    security: SecurityReport | None = None  # Phase 4

    pr_url: str | None = None
    error: str | None = None
    pushed: bool = False
    cancelled: bool = False
    budget: Budget = Field(default_factory=Budget)
    usage: Usage = Field(default_factory=Usage)
    waiting_s: float = 0.0  # time parked in AWAITING_INPUT, excluded from the budget
    seq: int = 0  # checkpoint sequence
    started_at: datetime | None = None

    # v1 kept a single synthetic task; v2 uses the graph. This keeps older call sites
    # and the Phase 1 tests working while the graph is the source of truth.
    @property
    def task(self) -> TaskSpec | None:
        if self.tasks is None or self.current_task_id is None:
            return None
        try:
            return self.tasks.by_id(self.current_task_id).spec
        except KeyError:
            return None

    @property
    def task_result(self) -> TaskResult | None:
        if self.current_task_id is None:
            return None
        return self.task_results.get(self.current_task_id)
