"""What counts as a file worth looking at.

One list, because three modules need it and three copies would drift: the repo map skips
these directories when it renders a tree, the symbol index skips them when it parses, and
the graph skips them when it resolves imports. A directory worth ignoring in one is worth
ignoring in all of them.

It lives here rather than in `repomap` because `symbols` needs it and `repomap` needs
`symbols` — the import used to run the other way and made a cycle the moment the map
started reading the index.
"""

from __future__ import annotations

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".autoswe",
        ".idea",
        ".vscode",
    }
)
