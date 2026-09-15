"""``run_tests``: run the repo's test command in the sandbox, parse the JSON report host-side."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from contracts import ToolResult
from tools.base import BaseTool, RunContext, schema
from tools.test_report import environment_report, parse_json_report, summarize

REPORT_REL = ".autoswe/report.json"
TIMEOUT_S = 600


def test_command(base: str, selector: str) -> str:
    sel = f" {selector.strip()}" if selector and selector.strip() else ""
    return f"{base}{sel} --json-report --json-report-file={REPORT_REL} -p no:cacheprovider"


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
        cmd = test_command(ctx.test_command, selector)
        report_path = ctx.worktree / REPORT_REL

        def _prepare() -> None:
            report_path.parent.mkdir(exist_ok=True)
            report_path.unlink(missing_ok=True)

        await asyncio.to_thread(_prepare)
        res = await ctx.sandbox.exec(cmd, timeout_s=TIMEOUT_S)
        output = (res.stdout + "\n" + res.stderr).strip()
        report = None
        if await asyncio.to_thread(report_path.is_file):
            try:
                data = json.loads(await asyncio.to_thread(report_path.read_text))
                report = parse_json_report(data, cmd, output, worktree=ctx.worktree)
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


def report_path(worktree: Path) -> Path:
    return worktree / REPORT_REL
