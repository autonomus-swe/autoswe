"""Every agent-to-orchestrator handoff is one of these Pydantic models (README §6)."""

from contracts.budget import Budget, Usage
from contracts.common import LLMModel, StateModel
from contracts.debug import DebugHypothesis
from contracts.events import Event, EventType
from contracts.plan import ImplementationPlan, Task, TaskGraph, TaskGraphSpec, TaskSpec, TaskStatus
from contracts.pr import PullRequestDescription
from contracts.repo import RepoFacts, RepoProfile
from contracts.review import ReviewCandidates, ReviewFinding, ReviewReport, Severity
from contracts.security import SecurityChecklist, SecurityFinding, SecurityReport, SecuritySeverity
from contracts.task_result import TaskResult
from contracts.testing import (
    FailureClassification,
    FailureKind,
    Frame,
    TestFailure,
    TestReport,
    Triage,
)
from contracts.tools import ExecResult, ToolResult

__all__ = [
    "Budget",
    "DebugHypothesis",
    "Event",
    "EventType",
    "ExecResult",
    "FailureClassification",
    "FailureKind",
    "Frame",
    "ImplementationPlan",
    "LLMModel",
    "PullRequestDescription",
    "RepoFacts",
    "RepoProfile",
    "ReviewCandidates",
    "ReviewFinding",
    "ReviewReport",
    "SecurityChecklist",
    "SecurityFinding",
    "SecurityReport",
    "SecuritySeverity",
    "Severity",
    "StateModel",
    "Task",
    "TaskGraph",
    "TaskGraphSpec",
    "TaskResult",
    "TaskSpec",
    "TaskStatus",
    "TestFailure",
    "TestReport",
    "ToolResult",
    "Triage",
    "Usage",
]
