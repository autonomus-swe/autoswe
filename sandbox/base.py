"""Sandbox protocol and output-capping helper shared by all implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from contracts import ExecResult

TRUNCATION_MARKER = "\n…[truncated {n} bytes]…\n"


class Sandbox(Protocol):
    id: str
    workspace: Path  # host path bind-mounted at /workspace
    # What this container is actually running. Read back rather than assumed: the factory
    # may have fallen back to the default when the chosen image was not built here, and a
    # run that pins the image it *asked* for would resume into the same failure.
    image: str

    async def start(self) -> None: ...

    async def exec(
        self,
        cmd: str,
        *,
        timeout_s: int = 120,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        max_output_bytes: int = 40_000,
    ) -> ExecResult: ...

    async def kill_exec(self) -> None:
        """Stop whatever is running inside, now.

        Called when a human cancels mid-command. There is no way to interrupt one exec
        and leave the rest usable — ``docker exec`` gives no handle to signal — so this
        kills the container. That is acceptable precisely because the only caller is a
        cancel: the run is over, and waiting out a ten-minute test suite to honour a stop
        request is worse than losing a sandbox that is about to be torn down anyway.
        """
        ...

    async def connect_install_network(self) -> None: ...

    async def set_cpus(self, cpus: float) -> None:
        """Change the CPU allowance of a running container.

        On the protocol because the orchestrator raises it for a compiling install and
        lowers it afterwards, and it holds a `Sandbox` rather than a `DockerSandbox`.
        Implementations that have no such dial may do nothing — a sandbox that cannot be
        throttled is not a sandbox that has failed.
        """
        ...

    async def disconnect_network(self) -> None: ...

    async def has_network(self) -> bool: ...

    async def stop(self, *, remove: bool = True) -> None: ...


def cap_output(data: bytes, max_bytes: int) -> tuple[str, bool]:
    """Keep the head and tail of ``data`` within ``max_bytes`` and mark the cut."""
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace"), False
    half = max_bytes // 2
    dropped = len(data) - 2 * half
    head = data[:half].decode("utf-8", errors="replace")
    tail = data[-half:].decode("utf-8", errors="replace")
    return head + TRUNCATION_MARKER.format(n=dropped) + tail, True
