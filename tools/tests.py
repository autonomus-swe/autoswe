"""``run_tests``: run the repo's test command in the sandbox, parse the JSON report host-side."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from contracts import ToolResult
from repo import stacks
from tools.base import BaseTool, RunContext, schema
from tools.junit import parse_junit
from tools.test_report import environment_report, parse_json_report, summarize

TIMEOUT_S = 600


def test_command(base: str, selector: str, stack: str = "python") -> str:
    """The repository's test command with a selector and a report destination.

    This used to staple pytest's flags onto whatever it was given, so a Go repository was
    asked to run `go test ./... --json-report --json-report-file=…`. What a selector means
    and where the report goes are both properties of the toolchain — see `repo/stacks`.
    """
    return stacks.by_name(stack).test_command(base, selector)


class RunTestsTool(BaseTool):
    name = "run_tests"
    description = (
        "Run the test suite inside the sandbox and get a parsed summary. Pass a pytest selector "
        "(file, directory or node id) to run a subset; omit it for the full suite."
    )
    input_schema = schema(
        {"selector": {"type": "string", "description": "Optional pytest selector."}}
    )
    mutating = True  # runs arbitrary repo code

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        selector = str(kwargs.get("selector") or "")
        stack = stacks.by_name(ctx.stack)
        cmd = test_command(ctx.test_command, selector, ctx.stack)
        report_path = ctx.worktree / stack.report_rel

        def _prepare() -> None:
            report_path.parent.mkdir(exist_ok=True)
            report_path.unlink(missing_ok=True)

        await asyncio.to_thread(_prepare)
        res = await ctx.sandbox.exec(cmd, timeout_s=TIMEOUT_S)
        output = (res.stdout + "\n" + res.stderr).strip()
        report = None
        if await asyncio.to_thread(report_path.is_file):
            raw = await asyncio.to_thread(report_path.read_text)
            try:
                if stack.report_format == "junit":
                    report = parse_junit(raw, cmd, output, worktree=ctx.worktree)
                else:
                    report = parse_json_report(json.loads(raw), cmd, output, worktree=ctx.worktree)
            except (json.JSONDecodeError, ValueError):
                report = None
        if report is None:
            why = "test run timed out" if res.timed_out else "no test report produced"
            report = environment_report(cmd, f"{why} (exit {res.exit_code})", output)
        content = summarize(report)
        if not report.passed and output:
            content += "\n\n--- output (tail) ---\n" + output[-2500:]
        return ToolResult(
            content=content, is_error=not report.passed, artifact=report.model_dump(mode="json")
        )


def report_path(worktree: Path, stack: str = "python") -> Path:
    return worktree / stacks.by_name(stack).report_rel
