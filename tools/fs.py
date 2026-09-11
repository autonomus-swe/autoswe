"""``read_file``: read-only, host-side, records the view hash so a later edit is not stale."""

from __future__ import annotations

import hashlib
from typing import Any

from contracts import ToolResult
from tools.base import BaseTool, RunContext, schema
from tools.editor import _numbered
from tools.policy import confine

DEFAULT_LINES = 400
BINARY_SNIFF_BYTES = 8192


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "Read a file under /workspace with line numbers. Returns the first 400 lines by "
        "default; pass start_line to continue through a longer file."
    )
    input_schema = schema(
        {
            "path": {"type": "string", "description": "Path relative to /workspace."},
            "start_line": {"type": "integer", "description": "1-based first line to show."},
            "end_line": {"type": "integer", "description": "1-based last line, inclusive."},
        },
        required=["path"],
    )
    mutating = False
    parallel_safe = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        target = confine(str(kwargs.get("path", "")), ctx.worktree)
        if target.is_dir():
            return ToolResult(
                content=f"error: {kwargs.get('path')} is a directory; use the editor's view",
                is_error=True,
            )
        if not target.is_file():
            return ToolResult(content=f"error: {kwargs.get('path')} does not exist", is_error=True)
        data = target.read_bytes()
        if looks_binary(data):
            return ToolResult(
                content=f"error: {kwargs.get('path')} looks binary ({len(data)} bytes)",
                is_error=True,
            )
        # the editor's staleness check keys off this, so reading then editing is allowed
        rel = str(target.relative_to(ctx.worktree.resolve()))
        ctx.view_hashes[rel] = hashlib.sha256(data).hexdigest()

        lines = data.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(1, int(kwargs.get("start_line") or 1))
        end_arg = kwargs.get("end_line")
        end = total if end_arg in (None, -1) else min(total, int(end_arg))
        if end < start:
            return ToolResult(content="error: end_line precedes start_line", is_error=True)
        capped = min(end, start + DEFAULT_LINES - 1)
        body = _numbered(lines[start - 1 : capped], start)
        note = ""
        if capped < total:
            shown = f"{start} to {capped} of {total}"
            note = f"\n… showing {shown}; pass start_line={capped + 1} to continue"
        return ToolResult(content=(body or "(empty file)") + note)
