"""`list_symbols`: what a file defines, without reading the file.

A read of a 900-line module costs the model the whole module. This costs it one line per
definition, which is usually all it needed to decide whether to read the file at all.

**The index is by base SHA; the Coder edits the worktree.** So a file the run has already
changed is parsed live rather than read from the table — an index that confidently
described the code as it was before the agent touched it would be worse than none.
"""

from __future__ import annotations

from typing import Any, ClassVar

from contracts import ToolResult
from observability.logging import get_logger
from tools.base import BaseTool, RunContext, schema

log = get_logger(__name__)

MAX_SYMBOLS = 200


class ListSymbolsTool(BaseTool):
    name = "list_symbols"
    description = (
        "List what a source file defines — functions, classes, methods, types — with the "
        "line each starts on. Much cheaper than reading the file, and usually enough to "
        "decide whether you need to."
    )
    input_schema = schema(
        {"path": {"type": "string", "description": "Repository-relative path to a source file."}},
        required=["path"],
    )
    mutating: ClassVar[bool] = False
    parallel_safe: ClassVar[bool] = True

    async def __call__(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        from repo.languages import for_path
        from repo.symbols import parse_file

        raw = str(kwargs.get("path", "")).strip()
        if not raw:
            return ToolResult(content="error: path is required", is_error=True)
        from tools.policy import confine

        target = confine(raw, ctx.worktree)
        rel = str(target.relative_to(ctx.worktree.resolve()))
        if for_path(target.suffix) is None:
            return ToolResult(content=f"{rel}: no symbol index for this file type", is_error=False)
        if not target.is_file():
            return ToolResult(content=f"error: {rel} does not exist", is_error=True)

        # Parsed from the worktree rather than read from the table. The table is keyed by
        # the base SHA and this file may have been edited since — see the module docstring.
        symbols = parse_file(ctx.worktree, rel)
        if not symbols:
            return ToolResult(content=f"{rel}: no definitions found")
        lines = [f"{rel}: {len(symbols)} definition(s)"]
        for sym in symbols[:MAX_SYMBOLS]:
            lines.append(f"  L{sym.start_line:<5} {sym.kind:<9} {sym.signature or sym.name}")
        if len(symbols) > MAX_SYMBOLS:
            lines.append(f"  …and {len(symbols) - MAX_SYMBOLS} more")
        return ToolResult(content="\n".join(lines))
