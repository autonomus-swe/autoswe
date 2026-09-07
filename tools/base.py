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
    view_hashes: dict[str, str] = field(default_factory=dict)  # editor staleness (path -> sha256)
    submitted: dict[str, Any] = field(default_factory=dict)  # payloads from submit_* tools


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
