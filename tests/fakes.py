"""Shared fakes for unit tests: an in-memory sandbox and a RunContext factory."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from contracts import ExecResult
from tools.base import RunContext

Handler = Callable[[str], ExecResult]


def ok(stdout: str = "", exit_code: int = 0, **kw: object) -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr="", duration_ms=1, **kw)


class FakeSandbox:
    """Scripted sandbox: ``handler(cmd)`` decides the result; records every command."""

    def __init__(self, workspace: Path, handler: Handler | None = None) -> None:
        self.id = "run-fake"
        self.workspace = workspace
        self.handler = handler or (lambda cmd: ok())
        self.commands: list[str] = []
        self.networked = True
        self.started = False
        self.stopped = False
        self.killed = False

    async def start(self) -> None:
        self.started = True

    async def exec(
        self,
        cmd: str,
        *,
        timeout_s: int = 120,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        max_output_bytes: int = 40_000,
    ) -> ExecResult:
        self.commands.append(cmd)
        return self.handler(cmd)

    async def kill_exec(self) -> None:
        self.killed = True

    async def connect_install_network(self) -> None:
        self.networked = True

    async def disconnect_network(self) -> None:
        self.networked = False

    async def has_network(self) -> bool:
        return self.networked

    async def stop(self, *, remove: bool = True) -> None:
        self.stopped = True


def make_ctx(worktree: Path, sandbox: FakeSandbox | None = None, **kw: object) -> RunContext:
    defaults: dict[str, object] = dict(
        run_id=uuid.uuid4(),
        step_id=uuid.uuid4(),
        role="coder",
        sandbox=sandbox or FakeSandbox(worktree),
        worktree=worktree,
        work_branch="agent/test",
        base_sha="HEAD",
        test_command="uv run --no-sync pytest -q",
    )
    defaults.update(kw)
    return RunContext(**defaults)  # type: ignore[arg-type]
