"""The run's diff: collected once, split per file, and budgeted before a model reads it.

The Reviewer works from the diff rather than from the repository, which is what keeps
Phase 4 independent of the symbol index Phase 5 adds. That makes this module the Reviewer's
entire view of the world, so three things matter more than they look:

**Untracked files are part of the change.** A run that adds a file and never commits it has
still changed the repository, and `git diff` alone would not show it. Collected with a
temporary index so the real one is never touched — an agent's uncommitted work must survive
being reviewed.

**A generated file is noise at review scale.** A 40,000-line lock file crowds out the
twelve lines that matter and costs the tokens that would have read them. Oversized and
generated files are replaced by a one-line summary *for the model*; the artifact keeps the
whole text, because a human asking "what actually changed" deserves the real answer.

**Line numbers come from the new side.** A review finding points at a line a reader can
open, which means the `+` side of the hunk. `added_lines` records exactly which those are,
and the security scanners reuse it to decide whether a finding is inside the change or was
already there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from repo.gitcmd import git

# A file diff longer than this is summarised for the model. Chosen to be larger than any
# hand-written change and smaller than any generated one.
MAX_FILE_DIFF_LINES = 1500
# Migrations are hand-written but often long and mechanical; the cut-off is lower.
MAX_MIGRATION_DIFF_LINES = 300

GENERATED = (
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
)
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".snap")

_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$", re.M)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


@dataclass
class FileDiff:
    """One file's worth of change, with the new-side line numbers kept."""

    path: str
    added: int
    removed: int
    text: str
    # Line numbers on the new side that this diff adds or changes. The Reviewer points at
    # these, and the scanners ask whether a finding falls inside one.
    added_lines: set[int] = field(default_factory=set)
    generated: bool = False

    @property
    def lines(self) -> int:
        return self.text.count("\n") + 1 if self.text else 0

    def summary(self) -> str:
        """One line, for when the text itself is not worth a model's attention."""
        why = "generated" if self.generated else f"{self.lines} lines"
        return f"{self.path}: +{self.added} -{self.removed} ({why}; full text in the diff artifact)"


def is_generated(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in GENERATED or path.endswith(GENERATED_SUFFIXES)


def too_large(fd: FileDiff) -> bool:
    """Should the model get a summary instead of the text?"""
    if fd.generated:
        return True
    limit = MAX_MIGRATION_DIFF_LINES if "/migrations/" in fd.path else MAX_FILE_DIFF_LINES
    return fd.lines > limit


async def full_diff(worktree: Path, base_sha: str) -> str:
    """Everything that changed since ``base_sha``, including files never added to git.

    Untracked files are staged into a *temporary* index with ``git add -N`` (intent to
    add), so they appear in the diff without the real index being touched. An agent's
    staged-or-not state is its own; reviewing must not disturb it.

    The index goes wherever git says its directory is, not in ``<worktree>/.git`` — in a
    linked worktree, which is what every run of this system works in, ``.git`` is a *file*
    containing a pointer and ``<worktree>/.git/anything`` cannot be created. That mistake
    cost an integration test; the unit tests had all used ``git init`` repositories, where
    ``.git`` is a directory and the bug is invisible.
    """
    gitdir = (await git("rev-parse", "--absolute-git-dir", cwd=worktree)).strip()
    index = Path(gitdir) / "autoswe-review-index"
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        # Start the temporary index from the current commit, or `add -N` would report every
        # tracked file as new.
        await git("read-tree", "HEAD", cwd=worktree, env=env)
        untracked = await git("ls-files", "--others", "--exclude-standard", cwd=worktree, env=env)
        paths = [p for p in untracked.splitlines() if p.strip()]
        if paths:
            await git("add", "-N", "--", *paths, cwd=worktree, env=env)
        return await git("diff", base_sha, cwd=worktree, env=env)
    finally:
        index.unlink(missing_ok=True)


def split_by_file(diff: str) -> list[FileDiff]:
    """One :class:`FileDiff` per file, in the order git emitted them."""
    if not diff.strip():
        return []
    starts = [(m.start(), m.group("b")) for m in _HEADER.finditer(diff)]
    out: list[FileDiff] = []
    for i, (start, path) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(diff)
        text = diff[start:end].rstrip("\n")
        out.append(_file_diff(path, text))
    return out


def _file_diff(path: str, text: str) -> FileDiff:
    added = removed = 0
    added_lines: set[int] = set()
    new_line = 0
    for line in text.splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            new_line = int(hunk.group("start"))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
            added_lines.add(new_line)
            new_line += 1
        elif line.startswith("-"):
            removed += 1
        elif new_line:  # context line, present on both sides
            new_line += 1
    return FileDiff(
        path=path,
        added=added,
        removed=removed,
        text=text,
        added_lines=added_lines,
        generated=is_generated(path),
    )


def diff_stat(files: list[FileDiff]) -> str:
    """``git diff --stat``-shaped summary, for a prompt header or a PR body."""
    if not files:
        return "no changes"
    rows = [f"  {f.path} | +{f.added} -{f.removed}" for f in files]
    total_a = sum(f.added for f in files)
    total_r = sum(f.removed for f in files)
    plural = "file" if len(files) == 1 else "files"
    return "\n".join(
        [*rows, f"  {len(files)} {plural} changed, {total_a} insertions(+), {total_r} deletions(-)"]
    )


def for_model(files: list[FileDiff]) -> str:
    """The diff as a model should see it: text for what was written, a line for the rest."""
    parts = []
    for f in files:
        parts.append(f.summary() if too_large(f) else f.text)
    return "\n\n".join(parts)


def touches(files: list[FileDiff], path: str, line: int) -> bool:
    """Is ``path:line`` inside something this change added?

    The scanners' ``in_diff`` tag, and the reason only new findings can block a run: a
    vulnerability the agent did not introduce is worth reporting and is not its to answer
    for.
    """
    for f in files:
        if f.path == path:
            return line in f.added_lines
    return False
