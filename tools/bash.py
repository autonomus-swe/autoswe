"""Shell access inside the sandbox. Each call is a fresh non-interactive shell."""

from __future__ import annotations

from typing import Any

from contracts import ToolResult
from tools.base import BaseTool, RunContext, schema
from tools.policy import check_bash

TIMEOUT_S = 120


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Run a shell command inside the sandbox at /workspace. Each call is a fresh shell: "
        "`cd` does not persist, so prefix commands with the directory you need. No network. "
        "git is not available here; use git_status, git_diff and git_commit instead."
    )
    input_schema = schema(
        {
            "command": {"type": "string", "description": "The command to run with bash -lc."},
            "restart": {
                "type": "boolean",
                "description": "Ignored; every call is already a fresh shell.",
            },
        },
        required=["command"],
    )
    anthropic_type = "bash_20250124"
    mutating = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        if kwargs.get("restart"):
            return ToolResult(
                content="session reset (each call is already a fresh shell in /workspace)"
            )
        command = str(kwargs.get("command", "")).strip()
        if not command:
            return ToolResult(content="error: empty command", is_error=True)
        check_bash(command)
        res = await ctx.sandbox.exec(command, timeout_s=TIMEOUT_S)
        parts = [res.stdout, res.stderr]
        if res.timed_out:
            parts.append(f"[timed out after {TIMEOUT_S}s]")
        elif res.exit_code != 0:
            parts.append(f"[exit code {res.exit_code}]")
        text = "\n".join(p for p in parts if p).strip() or "(no output)"
        return ToolResult(content=text, is_error=not res.ok)
