"""Read source lines from the worktree for the Debugger.

The code a hypothesis is formed from comes from disk, never from the model: a traceback
names a file and a line, and this reads what is actually there. That way a model cannot
quote code that does not exist and then reason about it — a failure mode worth designing
out rather than detecting.

Everything here is best-effort. A frame can name a file that was deleted, a line past the
end of it, or a binary; none of that should raise while a run is being diagnosed.
"""

from __future__ import annotations

from pathlib import Path

MAX_LINE_CHARS = 500
DEFAULT_RADIUS = 10
# Frames from the standard library and installed packages are not the agent's to fix.
VENDORED = ("/.venv/", "/site-packages/", "/dist-packages/", "/usr/lib/", "/usr/local/lib/")


def in_repo(path: str, worktree: Path) -> bool:
    """Is this frame in code the agent can edit?

    Resolved before comparing, so ``../`` in a traceback cannot smuggle a path outside
    the worktree past the check.
    """
    if not path:
        return False
    try:
        resolved = (worktree / path).resolve() if not Path(path).is_absolute() else Path(path)
        resolved = resolved.resolve()
        root = worktree.resolve()
    except (OSError, RuntimeError):  # RuntimeError: symlink loop
        return False
    if not resolved.is_relative_to(root):
        return False
    return not any(part in f"{resolved}" for part in VENDORED)


def relative(path: str, worktree: Path) -> str:
    """The path as the repository names it, or the original when it is outside."""
    if not path:
        return ""
    try:
        candidate = Path(path)
        resolved = (worktree / candidate).resolve() if not candidate.is_absolute() else candidate
        return str(resolved.resolve().relative_to(worktree.resolve()))
    except (OSError, ValueError, RuntimeError):
        return path


def _read(path: str, worktree: Path) -> list[str] | None:
    try:
        candidate = Path(path)
        target = candidate if candidate.is_absolute() else worktree / candidate
        if not target.is_file():
            return None
        return target.read_text(errors="replace").splitlines()
    except (OSError, UnicodeError):
        return None


def line_at(path: str, line: int, worktree: Path) -> str:
    """The single source line a frame points at, stripped, or "" when unavailable."""
    lines = _read(path, worktree)
    if lines is None or line < 1 or line > len(lines):
        return ""
    return lines[line - 1].strip()[:MAX_LINE_CHARS]


def around(path: str, line: int, worktree: Path, radius: int = DEFAULT_RADIUS) -> str:
    """Numbered lines around ``line``, with the failing one marked.

    The marker matters: a Debugger given twenty unlabelled lines has to count to find the
    one the traceback meant, and models miscount.
    """
    lines = _read(path, worktree)
    if lines is None:
        return ""
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    if start > end:
        return ""
    width = len(str(end))
    out = []
    for n in range(start, end + 1):
        marker = ">" if n == line else " "
        out.append(f"{marker} {n:>{width}} | {lines[n - 1][:MAX_LINE_CHARS]}")
    return "\n".join(out)
