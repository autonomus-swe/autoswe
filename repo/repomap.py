"""Repo map v1: a ranked file tree, budgeted to a line count.

Phase 5 replaces the scoring with tree-sitter symbols and centrality without changing
``render_map``'s signature.
"""

from __future__ import annotations

from pathlib import Path

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
MAX_FILE_BYTES = 1_000_000
BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".whl",
        ".so",
        ".dylib",
        ".dll",
        ".bin",
        ".pyc",
        ".lock",
    }
)
MANIFESTS = frozenset(
    {
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "Makefile",
        "requirements.txt",
        "setup.py",
        "Dockerfile",
        "README.md",
    }
)
SOURCE_DIRS = ("src", "app", "lib", "internal", "cmd", "pkg")


SOURCE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx", ".go", ".rs", ".rb", ".java", ".php"})


def _source_dirs(root: Path) -> set[str]:
    """Top-level directories that actually hold source, whatever they are called.

    Naming conventions vary — src/, app/, or flat packages like core/ and gateway/ — so
    the map ranks a directory by what is inside it rather than by its name.
    """
    found: set[str] = set()
    for path in root.rglob("*"):
        if path.suffix.lower() not in SOURCE_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts) or len(rel.parts) < 2:
            continue
        found.add(rel.parts[0])
    return found


def _rank(rel: Path, is_dir: bool, source_dirs: set[str]) -> tuple[int, str]:
    """Lower sorts first: manifests, source, other, tests, docs, dotfiles."""
    name = rel.name
    top = rel.parts[0] if rel.parts else name
    if not is_dir and rel.parent == Path(".") and name in MANIFESTS:
        return (0, name)
    if top in ("tests", "test", "spec"):
        return (3, str(rel))
    if top in ("docs", "doc", "examples"):
        return (4, str(rel))
    if top.startswith("."):  # config and tooling: real, but not what you read first
        return (5, str(rel))
    if top in source_dirs or top in SOURCE_DIRS:
        return (1, str(rel))
    return (2, str(rel))


def _keep(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        return path.stat().st_size <= MAX_FILE_BYTES
    except OSError:
        return False


def _human(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}K"
    return f"{size // (1024 * 1024)}M"


def render_map(worktree: Path, max_lines: int = 150) -> str:
    """An indented tree of the repository, most relevant first, within ``max_lines``."""
    root = Path(worktree)
    if not root.is_dir():
        return "(no worktree)"

    source_dirs = _source_dirs(root)
    entries: list[tuple[tuple[int, str], Path, bool, int]] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        if path.is_dir():
            entries.append((_rank(rel, True, source_dirs), rel, True, 0))
        elif _keep(path):
            entries.append((_rank(rel, False, source_dirs), rel, False, path.stat().st_size))

    if not entries:
        return "(empty repository)"

    entries.sort(key=lambda e: e[0])
    shown: list[str] = []
    per_dir_shown: dict[Path, int] = {}
    per_dir_total: dict[Path, int] = {}
    for _, rel, is_dir, _size in entries:
        if not is_dir:
            per_dir_total[rel.parent] = per_dir_total.get(rel.parent, 0) + 1

    for _rk, rel, is_dir, size in entries:
        if len(shown) >= max_lines:
            break
        indent = "  " * (len(rel.parts) - 1)
        if is_dir:
            shown.append(f"{indent}{rel.name}/")
            continue
        parent = rel.parent
        per_dir_shown[parent] = per_dir_shown.get(parent, 0) + 1
        shown.append(f"{indent}{rel.name}  {_human(size)}")

    omitted = sum(per_dir_total.values()) - sum(per_dir_shown.values())
    if omitted > 0:
        shown.append(f"… (+{omitted} more files)")
    return "\n".join(shown)
