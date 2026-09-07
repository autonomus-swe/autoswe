"""File viewer/editor. Runs host-side on the worktree; the model addresses files as
``/workspace/<path>`` or ``<path>``. ``str_replace``/``insert`` require a fresh ``view``."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from contracts import ToolResult
from tools.base import BaseTool, RunContext, schema
from tools.policy import confine

MAX_VIEW_LINES = 2000
SKIP_DIRS = {".git", ".venv", ".autoswe", "__pycache__", "node_modules", ".pytest_cache"}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(ctx: RunContext, target: Path) -> str:
    return str(target.relative_to(ctx.worktree.resolve()))


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{i:6}\t{line}" for i, line in enumerate(lines, start))


def _listing(root: Path) -> str:
    out: list[str] = []
    for level1 in sorted(root.iterdir()):
        if level1.name in SKIP_DIRS:
            continue
        out.append(level1.name + ("/" if level1.is_dir() else ""))
        if level1.is_dir():
            for level2 in sorted(level1.iterdir()):
                if level2.name in SKIP_DIRS:
                    continue
                out.append(f"  {level2.name}" + ("/" if level2.is_dir() else ""))
    return "\n".join(out) or "(empty directory)"


class EditorTool(BaseTool):
    name = "str_replace_based_edit_tool"
    description = (
        "View, create and edit files under /workspace. Commands: view (file or directory), "
        "create (new file), str_replace (replace exactly one occurrence of old_str), insert "
        "(insert new_str after line insert_line; 0 = top). Always view a file before editing it; "
        "edits are refused if the file changed since your last view."
    )
    input_schema = schema(
        {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]},
            "path": {"type": "string", "description": "Path relative to /workspace."},
            "file_text": {"type": "string", "description": "create: full file contents."},
            "old_str": {
                "type": "string",
                "description": "str_replace: text to replace (must occur exactly once).",
            },
            "new_str": {
                "type": "string",
                "description": "str_replace/insert: replacement or inserted text.",
            },
            "insert_line": {
                "type": "integer",
                "description": "insert: line number after which to insert.",
            },
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "view: [start, end] 1-based inclusive; end -1 = end of file.",
            },
        },
        required=["command", "path"],
    )
    anthropic_type = "text_editor_20250728"
    mutating = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command")
        target = confine(str(kwargs.get("path", "")), ctx.worktree)
        if command == "view":
            return self._view(ctx, target, kwargs.get("view_range"))
        if command == "create":
            return self._create(ctx, target, kwargs.get("file_text"))
        if command == "str_replace":
            return self._str_replace(ctx, target, kwargs.get("old_str"), kwargs.get("new_str"))
        if command == "insert":
            return self._insert(ctx, target, kwargs.get("insert_line"), kwargs.get("new_str"))
        return ToolResult(content=f"error: unknown command {command!r}", is_error=True)

    # ---- commands -------------------------------------------------------------

    def _view(self, ctx: RunContext, target: Path, view_range: Any) -> ToolResult:
        if target.is_dir():
            return ToolResult(content=_listing(target))
        if not target.is_file():
            return ToolResult(content=f"error: {_key(ctx, target)} does not exist", is_error=True)
        data = target.read_bytes()
        ctx.view_hashes[_key(ctx, target)] = _hash(data)
        lines = data.decode("utf-8", errors="replace").splitlines()
        start, end = 1, len(lines)
        if view_range:
            try:
                start, end = int(view_range[0]), int(view_range[1])
            except (TypeError, ValueError, IndexError):
                return ToolResult(content="error: view_range must be [start, end]", is_error=True)
            if end == -1 or end > len(lines):
                end = len(lines)
            if start < 1 or start > max(end, 1):
                return ToolResult(
                    content=f"error: invalid view_range for a {len(lines)}-line file", is_error=True
                )
        chunk = lines[start - 1 : end]
        note = ""
        if len(chunk) > MAX_VIEW_LINES:
            chunk = chunk[:MAX_VIEW_LINES]
            note = (
                f"\n… showing {MAX_VIEW_LINES} of {end - start + 1} lines; "
                "use view_range for the rest"
            )
        return ToolResult(content=_numbered(chunk, start) + note)

    def _create(self, ctx: RunContext, target: Path, file_text: Any) -> ToolResult:
        if file_text is None:
            return ToolResult(content="error: create requires file_text", is_error=True)
        key = _key(ctx, target)
        if target.exists() and key not in ctx.view_hashes:
            return ToolResult(
                content=(
                    f"error: {key} already exists; view it first, then use str_replace "
                    "or create to overwrite"
                ),
                is_error=True,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        data = str(file_text).encode()
        target.write_bytes(data)
        ctx.view_hashes[key] = _hash(data)
        return ToolResult(content=f"created {key} ({len(data)} bytes)")

    def _check_fresh(self, ctx: RunContext, target: Path) -> tuple[str, bytes] | ToolResult:
        key = _key(ctx, target)
        if not target.is_file():
            return ToolResult(content=f"error: {key} does not exist", is_error=True)
        data = target.read_bytes()
        seen = ctx.view_hashes.get(key)
        if seen is None:
            return ToolResult(content=f"error: view {key} before editing it", is_error=True)
        if seen != _hash(data):
            return ToolResult(
                content=f"error: {key} changed since you last viewed it; view it again",
                is_error=True,
            )
        return key, data

    def _str_replace(self, ctx: RunContext, target: Path, old: Any, new: Any) -> ToolResult:
        if not old:
            return ToolResult(
                content="error: str_replace requires a non-empty old_str", is_error=True
            )
        fresh = self._check_fresh(ctx, target)
        if isinstance(fresh, ToolResult):
            return fresh
        key, data = fresh
        text = data.decode("utf-8", errors="replace")
        count = text.count(str(old))
        if count != 1:
            why = "not found" if count == 0 else f"occurs {count} times; include more context"
            return ToolResult(content=f"error: old_str {why} in {key}", is_error=True)
        updated = text.replace(str(old), "" if new is None else str(new), 1)
        target.write_text(updated)
        ctx.view_hashes[key] = _hash(updated.encode())
        line_no = text[: text.index(str(old))].count("\n") + 1
        snippet = updated.splitlines()[max(0, line_no - 3) : line_no + 3]
        return ToolResult(content=f"edited {key}\n" + _numbered(snippet, max(1, line_no - 2)))

    def _insert(self, ctx: RunContext, target: Path, insert_line: Any, new: Any) -> ToolResult:
        if new is None or insert_line is None:
            return ToolResult(
                content="error: insert requires insert_line and new_str", is_error=True
            )
        fresh = self._check_fresh(ctx, target)
        if isinstance(fresh, ToolResult):
            return fresh
        key, data = fresh
        lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
        n = int(insert_line)
        if n < 0 or n > len(lines):
            return ToolResult(content=f"error: insert_line must be 0..{len(lines)}", is_error=True)
        block = str(new)
        if not block.endswith("\n"):
            block += "\n"
        lines[n:n] = [block]
        updated = "".join(lines)
        target.write_text(updated)
        ctx.view_hashes[key] = _hash(updated.encode())
        return ToolResult(content=f"inserted into {key} after line {n}")
