"""Cancelling a run that is in the middle of a long command.

The checks around each node are not enough: a phase spends most of its wall clock inside
one command — an install, a test suite — and a human who asks a run to stop should not
wait out a suite they no longer care about. Two halves here: the sandbox can be stopped
mid-command, and the watcher notices the flag and stops it.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from orchestrator.nodes import RunResources
from orchestrator.runner import _watch_for_cancel
from orchestrator.state import Phase, RunState
from sandbox.docker import EXIT_KILLED, DockerSandbox
from tests.fakes import FakeSandbox

pytestmark = pytest.mark.integration
IMAGE = "agent-sandbox:python-3.12"


def _docker_ready() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
        return True
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_ready(), reason=f"docker or image {IMAGE} unavailable (run `make sandbox-image`)"
)


@pytest.fixture
async def box(host_tmp: Path) -> AsyncIterator[DockerSandbox]:
    ws = host_tmp / "ws"
    ws.mkdir()
    sb = DockerSandbox(
        uuid.uuid4(),
        ws,
        image=IMAGE,
        network="agent-install",
        user=f"{os.getuid()}:{os.getgid()}",
    )
    await sb.start()
    try:
        yield sb
    finally:
        await sb.stop()


@requires_docker
async def test_a_long_command_stops_when_the_sandbox_is_killed(box: DockerSandbox) -> None:
    """Without this a cancel waits for the command's own timeout, which can be minutes."""
    started = time.monotonic()
    running = asyncio.create_task(box.exec("sleep 120", timeout_s=120))
    await asyncio.sleep(1.0)  # let the command actually start

    await box.kill_exec()
    result = await asyncio.wait_for(running, timeout=20)

    assert time.monotonic() - started < 20, "it did not wait out the 120s timeout"
    # Docker reports the exec's own status, so the kill comes back as 137 (SIGKILL)
    # rather than as an exception. A killed command reads as failed, not as a crash.
    assert result.exit_code == EXIT_KILLED and not result.ok
    assert not result.timed_out, "it was stopped, not slow"


@requires_docker
async def test_killing_is_safe_to_call_when_nothing_is_running(box: DockerSandbox) -> None:
    await box.kill_exec()
    await box.kill_exec()  # and twice: a cancel can race a finishing phase


# ---- the watcher ---------------------------------------------------------------------


class Bus:
    """Just the cancel flag, which is all the watcher reads."""

    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.checks = 0

    async def is_cancelled(self, run_id: Any) -> bool:
        self.checks += 1
        return self.cancelled


class Deps:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus


def state() -> RunState:
    return RunState(
        run_id=uuid.uuid4(),
        goal="g",
        repo_url="https://github.com/a/b",
        base_branch="main",
        work_branch="agent/x",
        phase=Phase.CODE,
    )


async def watch(deps: Any, s: RunState, res: RunResources, limit_s: float = 3.0) -> None:
    import orchestrator.runner as runner

    poll = runner.CANCEL_POLL_S
    runner.CANCEL_POLL_S = 0.05
    try:
        await asyncio.wait_for(_watch_for_cancel(s, deps, res), timeout=limit_s)
    finally:
        runner.CANCEL_POLL_S = poll


async def test_the_watcher_stops_the_sandbox_when_a_cancel_appears(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path)
    s, res = state(), RunResources(sandbox=sandbox)
    bus = Bus()
    deps = Deps(bus)

    async def cancel_soon() -> None:
        await asyncio.sleep(0.15)
        bus.cancelled = True

    await asyncio.gather(watch(deps, s, res), cancel_soon())
    assert sandbox.killed and s.cancelled


async def test_the_watcher_does_not_end_the_run_itself(tmp_path: Path) -> None:
    """It sets the flag and stops the sandbox; the runner is the only place a run ends."""
    sandbox = FakeSandbox(tmp_path)
    s, res = state(), RunResources(sandbox=sandbox)
    await watch(Deps(Bus(cancelled=True)), s, res)
    assert s.phase is Phase.CODE and s.error is None


async def test_a_sandbox_that_cannot_be_killed_does_not_mask_the_cancel(tmp_path: Path) -> None:
    class Stuck(FakeSandbox):
        async def kill_exec(self) -> None:
            raise RuntimeError("docker is gone")

    s, res = state(), RunResources(sandbox=Stuck(tmp_path))
    await watch(Deps(Bus(cancelled=True)), s, res)
    assert s.cancelled, "the flag is what ends the run, not the kill"


async def test_the_watcher_keeps_polling_while_the_run_is_wanted(tmp_path: Path) -> None:
    bus = Bus()
    with pytest.raises(TimeoutError):
        await watch(Deps(bus), state(), RunResources(sandbox=FakeSandbox(tmp_path)), limit_s=0.4)
    assert bus.checks > 1, "it polls rather than checking once and giving up"
