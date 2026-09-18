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
    # Who refused this call, when it was refused rather than run. `None` means it ran.
    #
    # It exists because the ledger could not previously tell the two apart: a forbidden
    # command and a command that ran and failed were both recorded `exit_code=1`, so
    # "did anything on the DENY list actually execute" was unanswerable from the audit
    # trail — which is the one question the trail exists to answer after a prompt
    # injection. `"policy"` is `tools/policy.py` refusing a forbidden command;
    # `"harness"` is a gate in `before_tool` (budget spent, hypothesis not yet stated).
    denied_by: str | None = None
