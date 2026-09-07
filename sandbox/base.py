"""Sandbox protocol and output-capping helper shared by all implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from contracts import ExecResult

TRUNCATION_MARKER = "\n…[truncated {n} bytes]…\n"


class Sandbox(Protocol):
    id: str
    workspace: Path  # host path bind-mounted at /workspace

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

    async def connect_install_network(self) -> None: ...

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
