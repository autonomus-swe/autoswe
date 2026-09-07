"""Docker implementation of :class:`sandbox.base.Sandbox`.

Container shape (README §4.5): non-root, read-only rootfs, tmpfs /tmp, only the worktree
bind-mounted, CPU/memory/pid limits, all capabilities dropped. The container is created on
the install network so ``uv sync`` can fetch packages, then disconnected; Docker refuses
``network connect`` on a container created with ``--network none``.

The docker SDK is synchronous; every call is wrapped in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
from docker.errors import APIError, NotFound
from docker.types import Mount

from contracts import ExecResult
from core.errors import SandboxError
from observability.logging import get_logger
from sandbox.base import cap_output

log = get_logger(__name__)

EXIT_TIMEOUT = 124  # coreutils `timeout`


class DockerSandbox:
    def __init__(
        self,
        run_id: UUID | str,
        workspace: Path,
        *,
        image: str,
        network: str,
        user: str,
        runtime: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.id = f"run-{run_id}"
        self.workspace = workspace
        self.image = image
        self.network = network
        self.user = user
        self.runtime = runtime
        self._client = client
        self.container: Any | None = None

    # ---- lifecycle -----------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def start(self) -> None:
        await asyncio.to_thread(self._start_sync)
        # HOME and the uv cache live on the /tmp tmpfs so any uid can write them.
        res = await self.exec("mkdir -p /tmp/home /tmp/uv", timeout_s=10)
        if not res.ok:
            raise SandboxError(f"could not prepare /tmp in {self.id}: {res.stderr}")
        log.info("sandbox_started", sandbox=self.id, image=self.image)

    def _start_sync(self) -> None:
        if not self.workspace.is_dir():
            raise SandboxError(f"workspace does not exist: {self.workspace}")
        try:
            self.client.networks.get(self.network)
        except NotFound:
            self.client.networks.create(self.network, driver="bridge")
        try:  # a crashed earlier run may have left a container with our name
            self.client.containers.get(self.id).remove(force=True)
        except NotFound:
            pass
        kwargs: dict[str, Any] = dict(
            command=["sleep", "infinity"],
            name=self.id,
            detach=True,
            user=self.user,
            read_only=True,
            tmpfs={"/tmp": "size=1g,exec"},  # noqa: S108 (container path)
            mounts=[Mount(target="/workspace", source=str(self.workspace.resolve()), type="bind")],
            network=self.network,
            mem_limit="4g",
            nano_cpus=2_000_000_000,
            pids_limit=512,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            environment={
                "HOME": "/tmp/home",  # noqa: S108 (container path)
                "UV_CACHE_DIR": "/tmp/uv",  # noqa: S108 (container path)
                "UV_PYTHON_DOWNLOADS": "never",
                "UV_LINK_MODE": "copy",
                "PIP_NO_CACHE_DIR": "1",
            },
            labels={"autoswe.run_id": self.id.removeprefix("run-")},
        )
        if self.runtime:
            kwargs["runtime"] = self.runtime
        try:
            self.container = self.client.containers.run(self.image, **kwargs)
        except APIError as e:
            raise SandboxError(f"docker run failed for {self.id}: {e.explanation}") from e

    async def stop(self, *, remove: bool = True) -> None:
        if self.container is None:
            return
        container = self.container

        def _stop() -> None:
            try:
                if remove:
                    container.remove(force=True)
                else:
                    container.stop(timeout=5)
            except NotFound:
                pass

        await asyncio.to_thread(_stop)
        if remove:
            self.container = None
        log.info("sandbox_stopped", sandbox=self.id, removed=remove)

    # ---- exec ----------------------------------------------------------------

    async def exec(
        self,
        cmd: str,
        *,
        timeout_s: int = 120,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        max_output_bytes: int = 40_000,
    ) -> ExecResult:
        if self.container is None:
            raise SandboxError(f"{self.id} is not running")
        container = self.container
        argv = ["timeout", "-k", "5", str(timeout_s), "bash", "-lc", cmd]
        t0 = time.monotonic()

        def _run() -> tuple[int, bytes, bytes]:
            code, (out, err) = container.exec_run(
                argv, demux=True, workdir=cwd, environment=env or {}
            )
            return int(code if code is not None else -1), out or b"", err or b""

        try:
            code, out, err = await asyncio.wait_for(asyncio.to_thread(_run), timeout_s + 30)
        except TimeoutError as e:  # the in-container `timeout` did not fire; treat as timed out
            raise SandboxError(f"exec hung past the timeout in {self.id}: {cmd[:80]}") from e
        duration_ms = int((time.monotonic() - t0) * 1000)
        stdout, t1 = cap_output(out, max_output_bytes)
        stderr, t2 = cap_output(err, max_output_bytes)
        return ExecResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            truncated=t1 or t2,
            timed_out=code == EXIT_TIMEOUT,
        )

    # ---- network -------------------------------------------------------------

    def _networks_sync(self) -> list[str]:
        assert self.container is not None
        self.container.reload()
        nets: dict[str, Any] = self.container.attrs["NetworkSettings"]["Networks"] or {}
        return list(nets)

    async def connect_install_network(self) -> None:
        if self.container is None:
            raise SandboxError(f"{self.id} is not running")
        container = self.container

        def _connect() -> None:
            if self.network not in self._networks_sync():
                self.client.networks.get(self.network).connect(container)

        await asyncio.to_thread(_connect)

    async def disconnect_network(self) -> None:
        if self.container is None:
            raise SandboxError(f"{self.id} is not running")
        container = self.container

        def _disconnect() -> None:
            for name in self._networks_sync():
                self.client.networks.get(name).disconnect(container, force=True)

        await asyncio.to_thread(_disconnect)
        log.info("sandbox_network_disconnected", sandbox=self.id)

    async def has_network(self) -> bool:
        if self.container is None:
            return False
        return bool(await asyncio.to_thread(self._networks_sync))
