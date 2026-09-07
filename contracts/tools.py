from __future__ import annotations

from typing import Any

from contracts.common import StateModel


class ExecResult(StateModel):
    """Outcome of one sandbox exec."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ToolResult(StateModel):
    """What a tool returns. ``content`` is what the model sees; ``artifact`` is for the harness."""

    content: str
    is_error: bool = False
    artifact: dict[str, Any] | None = None
