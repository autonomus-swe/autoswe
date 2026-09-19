"""Tool protocol and the per-step context every tool receives."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable
from uuid import UUID

from contracts import ToolResult
from sandbox.base import Sandbox
from storage.redis import RedisBus


@dataclass
class RunContext:
    run_id: UUID
    step_id: UUID
    role: str
    sandbox: Sandbox
    worktree: Path  # host path; all editor/git work happens here
    work_branch: str
    base_sha: str
    test_command: str
    bus: RedisBus | None = None
    # The database, for tools that read an index built earlier in the run. `None` in unit
    # tests and wherever a tool needs no index — `search_code` falls back to text search.
    engine: Any = None
    view_hashes: dict[str, str] = field(default_factory=dict)  # editor staleness (path -> sha256)
    submitted: dict[str, Any] = field(default_factory=dict)  # payloads from submit_* tools
    # Answers a human gave to `ask_user`, keyed by question. `before_tool` can only
    # refuse a call, never hand one a value, so an approved answer is left here for
    # the tool to collect.
    answers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[dict[str, Any]]
    anthropic_type: ClassVar[str | None]
    mutating: ClassVar[bool]
    parallel_safe: ClassVar[bool]
    requires_approval: ClassVar[bool]

    async def __call__(self, ctx: RunContext, **kwargs: Any) -> ToolResult: ...


class BaseTool:
    """Concrete base with safe defaults; subclasses set the class attributes and ``run``."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[dict[str, Any]]
    anthropic_type: ClassVar[str | None] = None
    mutating: ClassVar[bool] = True
    parallel_safe: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = False

    async def __call__(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        return await self.run(ctx, **kwargs)

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:  # pragma: no cover
        raise NotImplementedError


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Strict object schema in the shape both OpenAI-style and Anthropic tools accept."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
