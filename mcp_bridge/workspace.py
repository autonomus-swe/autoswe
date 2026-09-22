"""`search_code` and `read_file` over a run's worktree, for callers outside the run.

An editor driving a run over MCP wants to look at what the agent is working on without
cloning it. Both tools already exist — the agents use them every step — so this module
builds the context they expect rather than reimplementing a search and a file read. That
matters most for `read_file`: its path handling goes through `tools.policy.confine`, which
is what stops `../../.ssh/id_rsa` from being a valid argument. A hand-rolled `open()` here
would be a second path check to keep in step with that one, and the version that drifts is
always the copy.

The worktree is the worker's, at `<worktrees_dir>/<run_id>`. It exists while the run is
running and for as long afterwards as the collector leaves it; when it is gone this says
so rather than returning an empty result, because "no matches" and "nothing to search" are
different answers and only one of them is worth acting on.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, NoReturn, cast

from contracts import ExecResult, ToolResult
from core.errors import PolicyViolation
from sandbox.base import Sandbox
from tools.base import RunContext
from tools.fs import ReadFileTool
from tools.search import SearchCodeTool

# A step id is recorded against tool calls the orchestrator persists. Nothing here
# persists anything, so this is a constant rather than a fresh uuid4 per call: a random id
# in a log would look like a step that existed.
NO_STEP = uuid.UUID(int=0)


class NoSandbox:
    """Stands in for the container these two tools never touch.

    `RunContext` requires a sandbox because most tools need one. `read_file` and
    `search_code` are host-side and read-only, so there is nothing to give them. Every
    member raises instead of quietly doing nothing: if a tool is ever added to this path
    that does reach for the sandbox, the failure names the reason rather than producing a
    result computed from a no-op.
    """

    id = "no-sandbox"
    workspace = Path("/nonexistent")
    image = "none"

    def _refuse(self) -> NoReturn:
        raise RuntimeError(
            "this context has no sandbox: the MCP workspace tools are read-only and "
            "host-side, and never execute anything"
        )

    async def start(self) -> None:
        self._refuse()

    async def exec(
        self,
        cmd: str,
        *,
        timeout_s: int = 120,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        max_output_bytes: int = 40_000,
    ) -> ExecResult:
        self._refuse()

    async def kill_exec(self) -> None:
        self._refuse()

    async def connect_install_network(self) -> None:
        self._refuse()

    async def set_cpus(self, cpus: float) -> None:
        self._refuse()

    async def disconnect_network(self) -> None:
        self._refuse()

    async def has_network(self) -> bool:
        self._refuse()

    async def stop(self, *, remove: bool = True) -> None:
        self._refuse()


def worktree_for(worktrees_dir: Path, run_id: uuid.UUID) -> Path:
    """Where the worker put this run's checkout. Same rule as `Deps.worktrees_dir`."""
    return Path(os.path.expanduser(str(worktrees_dir))) / str(run_id)


def context_for(row: Any, *, worktrees_dir: Path, engine: Any = None) -> RunContext:
    """A read-only context for one run, with the run's real branch and base commit.

    Those two are not decoration: `search_code`'s semantic mode looks up the embedding
    index by `base_sha`, so a context that invented one would silently fall back to text
    search and the caller would never know the index had been there all along.
    """
    return RunContext(
        run_id=row.id,
        step_id=NO_STEP,
        role="mcp",
        sandbox=cast(Sandbox, NoSandbox()),
        worktree=worktree_for(worktrees_dir, row.id),
        work_branch=row.work_branch,
        base_sha=row.base_sha or "",
        test_command="",
        bus=None,
        engine=engine,
    )


async def read_file(ctx: RunContext, **kwargs: Any) -> ToolResult:
    """`tools.fs.ReadFileTool`, with a missing worktree reported as such."""
    missing = _missing(ctx)
    if missing is not None:
        return missing
    try:
        return await ReadFileTool()(ctx, **kwargs)
    except PolicyViolation as e:
        # `confine` raises rather than returning; over MCP that would surface as an
        # unexplained crash, and "your path escaped the workspace" is precisely the
        # feedback the caller needs.
        return ToolResult(content=f"error: {e}", is_error=True)


async def search_code(ctx: RunContext, **kwargs: Any) -> ToolResult:
    """`tools.search.SearchCodeTool`, with a missing worktree reported as such."""
    missing = _missing(ctx)
    if missing is not None:
        return missing
    return await SearchCodeTool()(ctx, **kwargs)


def _missing(ctx: RunContext) -> ToolResult | None:
    if ctx.worktree.is_dir():
        return None
    return ToolResult(
        content=(
            f"error: no worktree for run {ctx.run_id} at {ctx.worktree}. It is removed once "
            "the run is collected, and it only ever exists on the machine running the "
            "worker — read the `diff` artifact instead."
        ),
        is_error=True,
    )
