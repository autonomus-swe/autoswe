"""Defense in depth for model-issued commands and paths. The real boundaries are the
sandbox (no network, non-root, read-only rootfs, one bind mount); this list catches the
obvious mistakes early and tells the model what to do instead."""

from __future__ import annotations

import re
from pathlib import Path

from core.errors import PolicyViolation

DENY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p), why)
    for p, why in [
        (r"\bgit\b", "git is not available in bash; use git_status / git_diff / git_commit"),
        (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+/(\s|$)", "refusing to delete /"),
        (r"(^|[\s;&|])(sudo|su)\b", "no privilege escalation"),
        (r"(curl|wget)[^|;&]*\|\s*(ba|z|da)?sh\b", "no piping downloads into a shell"),
        (r"--force(?![-\w])|\s-f\b.*\bpush\b", "no force operations"),
        (r"\bmkfs\b|\bdd\s+if=|:\(\)\s*\{", "destructive command"),
        (r"\bchmod\s+(-R\s+)?777\b", "world-writable permissions"),
    ]
]

FORBIDDEN_PARTS = frozenset({".git", ".autoswe"})


def check_bash(cmd: str) -> None:
    """Raise :class:`PolicyViolation` with the reason and the replacement, if any."""
    for pattern, why in DENY:
        if pattern.search(cmd):
            raise PolicyViolation(why)


def confine(path: str, root: Path) -> Path:
    """Resolve ``path`` inside ``root``; refuse escapes, symlinks out, and internal dirs.

    Accepts the container-side ``/workspace/...`` spelling and relative paths.
    """
    raw = path.strip()
    if raw.startswith("/workspace"):
        raw = raw[len("/workspace") :].lstrip("/")
    elif raw.startswith("/"):
        raise PolicyViolation(f"absolute paths outside /workspace are not allowed: {path}")
    root_resolved = root.resolve()
    candidate = (root_resolved / raw).resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise PolicyViolation(f"path escapes the workspace: {path}")
    rel_parts = candidate.relative_to(root_resolved).parts
    if any(part in FORBIDDEN_PARTS for part in rel_parts):
        raise PolicyViolation(f"path is inside a protected directory: {path}")
    return candidate
