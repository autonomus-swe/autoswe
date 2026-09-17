"""Shared fakes for unit tests: an in-memory sandbox, a RunContext factory, a test key."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path

from contracts import ExecResult
from tools.base import RunContext

Handler = Callable[[str], ExecResult]


def planted_secret(tag: str) -> str:
    """A key gitleaks will flag, derived rather than written down.

    Written as a literal it trips this repository's own pre-commit gitleaks hook — which is
    the hook being right: it cannot tell a fixture from a real key. Allowlisting the files
    that need one would hide a real key pasted there later, so this derives it instead:
    deterministic, no literal in any source file, and verified against gitleaks to actually
    trip `generic-api-key` rather than assumed to. Forty hex characters clear its entropy
    threshold.

    Lives here because two test modules need one, and the second copy is how I walked into
    the same blocked commit twice.
    """
    return hashlib.sha256(f"autoswe-scanner-test-{tag}".encode()).hexdigest()[:40]


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
