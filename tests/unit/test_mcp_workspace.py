"""Reading a run's checkout from outside the run.

The point of routing these through `tools.fs` and `tools.search` rather than writing an
`open()` here is that the agents' path containment comes with them. These tests are the
evidence that it did: an MCP server is reachable from an editor, so `read_file` is a
file-read primitive exposed to whatever the model on the other end decides to ask for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mcp_bridge import workspace

pytestmark = pytest.mark.unit

RUN_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


@dataclass
class Row:
    id: uuid.UUID = RUN_ID
    work_branch: str = f"agent/{RUN_ID}"
    base_sha: str | None = "abc123"


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A worktrees root with one run's checkout in it, and a secret outside."""
    root = tmp_path / "worktrees"
    checkout = root / str(RUN_ID)
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "secrets.txt").write_text("hunter2\n")
    return root


def ctx(worktrees_dir: Path) -> Any:
    return workspace.context_for(Row(), worktrees_dir=worktrees_dir)


async def test_reads_a_file_from_the_runs_checkout(worktree: Path) -> None:
    result = await workspace.read_file(ctx(worktree), path="src/calc.py")
    assert not result.is_error and "def add(a, b)" in result.content


async def test_searches_the_runs_checkout(worktree: Path) -> None:
    result = await workspace.search_code(ctx(worktree), pattern="def add", max_results=10)
    assert not result.is_error and "src/calc.py" in result.content


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.txt",
        "src/../../secrets.txt",
        "/etc/passwd",
        "/workspace/../secrets.txt",
    ],
)
async def test_refuses_to_read_outside_the_checkout(worktree: Path, path: str) -> None:
    """`confine` raises rather than returning, and an exception out of a tool reaches the
    client as "Error executing tool read_file" with no reason. Caught and reported."""
    result = await workspace.read_file(ctx(worktree), path=path)
    assert result.is_error
    assert "hunter2" not in result.content
    assert "escapes the workspace" in result.content or "absolute paths" in result.content


async def test_a_missing_worktree_says_so_rather_than_returning_nothing(tmp_path: Path) -> None:
    """A run collected an hour ago and a run whose file genuinely has no matches are
    different answers. Returning "no matches" for the first sends the caller looking for
    a bug in code it cannot see."""
    empty = tmp_path / "none"
    empty.mkdir()
    for result in (
        await workspace.read_file(ctx(empty), path="src/calc.py"),
        await workspace.search_code(ctx(empty), pattern="def add"),
    ):
        assert result.is_error and "no worktree for run" in result.content


async def test_the_context_carries_the_runs_real_branch_and_commit(worktree: Path) -> None:
    """Not decoration: `search_code`'s semantic mode looks the embedding index up by
    `base_sha`, so an invented one silently degrades to text search."""
    built = workspace.context_for(Row(), worktrees_dir=worktree)
    assert built.base_sha == "abc123"
    assert built.work_branch == f"agent/{RUN_ID}"
    assert built.worktree == worktree / str(RUN_ID)


async def test_the_sandbox_stand_in_refuses_rather_than_pretending() -> None:
    """These two tools never reach for a sandbox. If one ever does, the failure should
    name the reason instead of returning a result computed from a silent no-op."""
    sandbox = workspace.NoSandbox()
    with pytest.raises(RuntimeError, match="no sandbox"):
        await sandbox.exec("ls")
    with pytest.raises(RuntimeError, match="no sandbox"):
        await sandbox.start()
