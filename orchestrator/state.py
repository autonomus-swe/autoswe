"""Run state and the full phase enum. Later phases add nodes, not enum values."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
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

    # ---- Phase 3: the verification loop ----
    # Report signature of the previous TEST. Equal signatures mean the last debug attempt
    # changed nothing, which is what puts the Debugger on an alternative strategy.
    previous_failure_signature: str | None = None
    strategy: Literal["alternative"] | None = None
    # Why the run is escalating. escalate_node reads this to decide what to do next.
    escalation_reason: str | None = None
    # Failures already present on the base branch, recorded in SETUP. The agent owns the
    # tests it touched, not the ones it inherited.
    baseline_failures: set[str] = Field(default_factory=set)
    # Where to continue after AWAITING_INPUT or ESCALATE: PLAN, CODE, DEBUG or FAILED.
    resume_phase: Phase | None = None
    # Source context per failing test, for the Debugger prompt. Runtime only, never in an
    # LLM schema — the model reads code through frames, not through this.
    test_context: dict[str, str] = Field(default_factory=dict)
    # Budget kinds already warned about, so a warning is emitted once and not every node.
    warned: set[str] = Field(default_factory=set)
    # Whether spend can be measured at all for the model in use. Set once from the
    # pricing table; kept on the state so `transition` stays a function of `s` alone.
    cost_measurable: bool = True

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

    def elapsed_s(self, now: datetime | None = None) -> float:
        """Seconds this run has been working, excluding time parked on a human.

        A run waiting overnight for an answer has not spent its wall-clock budget; the
        budget is meant to bound the agent, not the reviewer.
        """
        if self.started_at is None:
            return 0.0
        moment = now or datetime.now(UTC)
        return max(0.0, (moment - self.started_at).total_seconds() - self.waiting_s)

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
