"""``search_code``: ripgrep over the worktree, host-side, read-only."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contracts import ToolResult
from tools.base import BaseTool, RunContext, schema

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".autoswe",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)
SKIP_GLOBS = tuple(f"!{d}/*" for d in sorted(SKIP_DIRS))
TIMEOUT_S = 30
HARD_CAP = 200
MAX_FILE_BYTES = 2_000_000


def ripgrep_path() -> str | None:
    """The ripgrep binary, when one is installed. A shell function does not count."""
    return shutil.which("rg")


def _skipped(rel: Path) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts)


def python_search(
    root: Path, pattern: str, *, glob: str | None, max_results: int, fixed_string: bool
) -> tuple[list[str], int]:
    """Pure-Python fallback so the tool works without ripgrep installed.

    Slower than ripgrep on large trees, but dependency-free and identical in output, so a
    missing binary degrades speed rather than breaking the agent.
    """
    matcher: Callable[[str], bool]
    if fixed_string:
        matcher = lambda line: pattern in line  # noqa: E731
    else:
        compiled = re.compile(pattern)
        matcher = lambda line: compiled.search(line) is not None  # noqa: E731
    hits: list[str] = []
    seen = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _skipped(rel):
            continue
        if glob and not fnmatch.fnmatch(str(rel), glob):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if not matcher(line):
                continue
            seen += 1
            if len(hits) < max_results:
                hits.append(f"{rel}:{number}: {line}")
    return hits, max(0, seen - len(hits))


def parse_rg_json(stdout: str, max_results: int) -> tuple[list[str], int]:
    """``(rendered matches, how many were omitted)`` from ``rg --json`` output."""
    hits: list[str] = []
    seen = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "match":
            continue
        seen += 1
        if len(hits) >= max_results:
            continue
        data = event["data"]
        path = data.get("path", {}).get("text", "?")
        number = data.get("line_number", 0)
        text = (data.get("lines", {}).get("text") or "").rstrip("\n")
        hits.append(f"{path}:{number}: {text}")
    return hits, max(0, seen - len(hits))


class SearchCodeTool(BaseTool):
    name = "search_code"
    description = (
        "Search the repository with ripgrep. Returns path:line: text entries. Use a regular "
        "expression, or set fixed_string for a literal. Never searches .git or .venv."
    )
    input_schema = schema(
        {
            "pattern": {"type": "string", "description": "Regex, or a literal with fixed_string."},
            "glob": {"type": "string", "description": "Restrict to paths, e.g. '*.py'."},
            "max_results": {"type": "integer", "description": "Default 50."},
            "fixed_string": {"type": "boolean", "description": "Treat pattern as a literal."},
            "context": {"type": "integer", "description": "Lines of context, default 0."},
        },
        required=["pattern"],
    )
    mutating = False
    parallel_safe = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        pattern = str(kwargs.get("pattern", ""))
        if not pattern:
            return ToolResult(content="error: pattern is required", is_error=True)
        max_results = min(int(kwargs.get("max_results") or 50), HARD_CAP)
        rg = ripgrep_path()
        if rg is None:  # ripgrep is a fast path, not a requirement
            hits, omitted = await asyncio.to_thread(
                python_search,
                ctx.worktree,
                pattern,
                glob=str(kwargs["glob"]) if kwargs.get("glob") else None,
                max_results=max_results,
                fixed_string=bool(kwargs.get("fixed_string")),
            )
            return self._render(pattern, hits, omitted)

        argv = [rg, "--json", "--no-messages"]
        if kwargs.get("fixed_string"):
            argv.append("--fixed-strings")
        if kwargs.get("context"):
            argv += ["--context", str(int(kwargs["context"]))]
        if kwargs.get("glob"):
            argv += ["--glob", str(kwargs["glob"])]
        for skip in SKIP_GLOBS:
            argv += ["--glob", skip]
        argv += ["-e", pattern, "."]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(ctx.worktree),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            return ToolResult(content=f"error: search timed out after {TIMEOUT_S}s", is_error=True)
        if proc.returncode not in (0, 1):  # 1 simply means no matches
            detail = err.decode(errors="replace").strip()[:300]
            return ToolResult(content=f"error: ripgrep failed: {detail}", is_error=True)

        hits, omitted = parse_rg_json(out.decode(errors="replace"), max_results)
        return self._render(pattern, hits, omitted)

    @staticmethod
    def _render(pattern: str, hits: list[str], omitted: int) -> ToolResult:
        if not hits:
            return ToolResult(content=f"no matches for {pattern!r}")
        body = "\n".join(hits)
        if omitted:
            body += f"\n… {omitted} more matches omitted; narrow the pattern or pass a glob"
        return ToolResult(content=body)
