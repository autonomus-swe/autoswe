"""Request and response bodies. Strict: unknown fields are rejected at the boundary."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts import Budget

ALLOWED_HOSTS = frozenset({"github.com", "www.github.com"})


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_url: str = Field(max_length=500)
    goal: str = Field(min_length=10, max_length=4000)
    base_branch: str = Field(default="main", max_length=200)
    budget: Budget | None = None

    @field_validator("repo_url")
    @classmethod
    def only_github(cls, v: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(v.strip())
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("repo_url must be an https://github.com/... URL")
        if len([p for p in parsed.path.split("/") if p]) < 2:
            raise ValueError("repo_url must include owner and repository")
        return v.strip()

    @field_validator("base_branch")
    @classmethod
    def plain_branch(cls, v: str) -> str:
        v = v.strip()
        if not v or v.startswith("-") or any(c in v for c in " \t~^:?*[\\"):
            raise ValueError("base_branch is not a valid branch name")
        return v


class RunAccepted(BaseModel):
    run_id: UUID


class RunSummary(BaseModel):
    run_id: UUID
    phase: str
    status: str
    goal: str
    repo_url: str
    work_branch: str
    cost_usd: float
    pr_url: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> RunSummary:
        cost = (
            row.cost_usd if isinstance(row.cost_usd, Decimal) else Decimal(str(row.cost_usd or 0))
        )
        return cls(
            run_id=row.id,
            phase=row.phase,
            status=row.status,
            goal=row.goal,
            repo_url=row.repo_url,
            work_branch=row.work_branch,
            cost_usd=float(cost),
            pr_url=row.pr_url,
            error=row.error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class TaskView(BaseModel):
    """A task as the UI shows it."""

    id: str
    title: str
    status: str
    depends_on: list[str]
    files: list[str]
    acceptance_criteria: list[str]
    test_selector: str
    attempts: int

    @classmethod
    def from_row(cls, row: Any) -> TaskView:
        return cls(
            id=row.id,
            title=row.title,
            status=row.status,
            depends_on=list(row.depends_on or []),
            files=list(row.files or []),
            acceptance_criteria=list(row.acceptance_criteria or []),
            test_selector=row.test_selector or "",
            attempts=row.attempts,
        )


class StepView(BaseModel):
    id: UUID
    agent: str
    phase: str
    task_id: str | None
    attempt: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class ToolCallView(BaseModel):
    seq: int
    name: str
    exit_code: int | None
    duration_ms: int
    input: dict[str, Any]
    output_preview: str | None


class LLMCallView(BaseModel):
    seq: int
    model: str
    effort: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    stop_reason: str | None


class EventView(BaseModel):
    """A recorded event. Carries the time it happened, which an SSE frame cannot."""

    id: int
    type: str
    payload: dict[str, Any]
    ts: datetime


class ArtifactView(BaseModel):
    """One artifact row, without its content. `size` is of the serialised JSON, because
    that is what a caller fetching it will receive."""

    kind: str
    path: str | None
    created_at: datetime
    size: int


class RunDetail(BaseModel):
    """Everything the UI needs for one run in a single request."""

    run: RunSummary
    tasks: list[TaskView]
    steps: list[StepView]
    events: list[EventView]
    tool_calls: list[ToolCallView]
    llm_calls: list[LLMCallView]
    totals: dict[str, float]
