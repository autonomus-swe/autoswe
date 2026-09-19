"""Docker implementation of :class:`sandbox.base.Sandbox`.

Container shape (README §4.5): non-root, read-only rootfs, tmpfs /tmp, only the worktree
bind-mounted, CPU/memory/pid limits, all capabilities dropped. The container is created on
the install network so ``uv sync`` can fetch packages, then disconnected; Docker refuses
``network connect`` on a container created with ``--network none``.

The docker SDK is synchronous; every call is wrapped in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
from docker.errors import APIError, NotFound
from docker.types import Mount

from contracts import ExecResult
from core.errors import SandboxError
from observability import metrics
from observability.logging import get_logger
from observability.tracing import annotate, trace_span
from sandbox.base import cap_output

log = get_logger(__name__)

EXIT_TIMEOUT = 124  # coreutils `timeout`
EXIT_KILLED = 137  # 128 + SIGKILL, what a killed container reports
START_TIMEOUT_S = 15.0
START_POLL_S = 0.15
START_SETTLE_SAMPLES = 4  # ~0.6s of continuous "running" before we trust it
# The CFS period every CPU limit here is expressed against. `cpus` * this = the quota.
CPU_PERIOD_US = 100_000


def image_present(tag: str, client: Any | None = None) -> bool:
    """Whether this image is already on the host.

    Asked before a container is created, because `containers.run` on a missing tag does
    not fail — it tries to *pull* it. These tags are built locally and exist in no
    registry, so the pull fails with "pull access denied ... may require 'docker login'",
    which is converted to a SandboxError and kills the run in SETUP. The real problem is a
    missing local image and the message names authentication.

    An unreachable daemon answers False, which routes the caller to its fallback; the
    daemon being down is a failure that surfaces more clearly a moment later at `run`.
    """
    try:
        (client or docker.from_env()).images.get(tag)
        return True
    except Exception:
        return False


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
        no_new_privileges: bool = True,
        mem_limit: str = "4g",
        tmpfs_size: str = "1g",
        cpus: float = 2.0,
        client: Any | None = None,
    ) -> None:
        self.id = f"run-{run_id}"
        self.workspace = workspace
        self.image = image
        self.mem_limit = mem_limit
        self.tmpfs_size = tmpfs_size
        self.cpus = cpus
        self.network = network
        self.user = user
        self.runtime = runtime
        self.no_new_privileges = no_new_privileges
        self.effective_no_new_privileges = no_new_privileges
        self._client = client
        self.container: Any | None = None

    # ---- lifecycle -----------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _quota(self, cpus: float) -> int:
        """CFS quota for a CPU count, clamped to what this host actually has.

        Docker validates a CPU limit against the host's core count and refuses anything
        larger — measured: asking for 24 CPUs on a 12-CPU host is a 400 with "range of CPUs
        is from 0.01 to 12.00". The sandbox turns an APIError into a SandboxError, so an
        unclamped request would kill the run in SETUP on any machine smaller than the one
        the limit was chosen on, which includes most CI runners.

        Memory gets no such treatment because Docker does not validate it: a 64g limit is
        accepted without comment on a 38g host. A ceiling that is never reached is harmless;
        one that is reached kills the container with 137, which is already handled.
        """
        available = self._host_cpus()
        asked = max(0.01, cpus)
        if available and asked > available:
            log.info("sandbox_cpus_clamped", asked=asked, host_cpus=available)
            asked = float(available)
        return int(asked * CPU_PERIOD_US)

    def _host_cpus(self) -> int:
        try:
            return int(self.client.info().get("NCPU") or 0)
        except Exception:  # an unreachable daemon fails later, and more clearly, at run
            return 0

    async def set_cpus(self, cpus: float) -> None:
        """Change this container's CPU allowance while it runs.

        Used to give a compiling install more of the machine and take it back afterwards.
        Never raises: a limit that could not be adjusted leaves the container running at
        the one it already had, which is slower and entirely correct.
        """
        if self.container is None:
            return
        container = self.container
        quota = self._quota(cpus)

        def _update() -> None:
            container.update(cpu_period=CPU_PERIOD_US, cpu_quota=quota)

        try:
            await asyncio.to_thread(_update)
            log.info("sandbox_cpus_changed", sandbox=self.id, cpus=cpus)
        except Exception as e:
            log.warning("sandbox_cpus_unchanged", sandbox=self.id, error=f"{type(e).__name__}: {e}")

    async def start(self) -> None:
        await asyncio.to_thread(self._start_sync, self.no_new_privileges)
        # HOME and the uv cache live on the /tmp tmpfs so any uid can write them.
        res = await self.exec("mkdir -p /tmp/home /tmp/uv", timeout_s=10)
        if not res.ok:
            raise SandboxError(f"could not prepare /tmp in {self.id}: {res.stderr}")
        log.info("sandbox_started", sandbox=self.id, image=self.image)

    def _start_sync(self, no_new_privileges: bool) -> None:
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
            # Charged against `mem_limit`, so the two scale together — see sandbox/select.
            tmpfs={"/tmp": f"size={self.tmpfs_size},exec"},  # noqa: S108 (container path)
            mounts=[Mount(target="/workspace", source=str(self.workspace.resolve()), type="bind")],
            network=self.network,
            mem_limit=self.mem_limit,
            # `cpu_quota`/`cpu_period` rather than `nano_cpus`, which is the same cgroup
            # value — measured: both produce `cpu.max` of "200000 100000" — but only these
            # two are accepted by `container.update()`. A container created with
            # `nano_cpus` can be changed afterwards only through an undocumented raw POST,
            # which is not something to depend on for the install-time bump below.
            cpu_period=CPU_PERIOD_US,
            cpu_quota=self._quota(self.cpus),
            pids_limit=512,
            cap_drop=["ALL"],
            environment={
                "HOME": "/tmp/home",  # noqa: S108 (container path)
                "UV_CACHE_DIR": "/tmp/uv",  # noqa: S108 (container path)
                "UV_PYTHON_DOWNLOADS": "never",
                "UV_LINK_MODE": "copy",
                "PIP_NO_CACHE_DIR": "1",
            },
            labels={"autoswe.run_id": self.id.removeprefix("run-")},
        )
        if no_new_privileges:
            kwargs["security_opt"] = ["no-new-privileges:true"]
        if self.runtime:
            kwargs["runtime"] = self.runtime
        try:
            self.container = self.client.containers.run(self.image, **kwargs)
        except APIError as e:
            hint = ""
            if "bind source path does not exist" in str(e.explanation):
                hint = (
                    " (the Docker daemon cannot see this host path; with snap-packaged Docker, "
                    "WORKTREES_DIR must live under your home directory)"
                )
            raise SandboxError(f"docker run failed for {self.id}: {e.explanation}{hint}") from e
        why = self._exit_reason()
        if why is None:
            self.effective_no_new_privileges = no_new_privileges
            return
        # Some Docker builds refuse to exec anything under no_new_privs because their
        # AppArmor profile transition needs it off. Retry once without it; the other
        # boundaries (non-root, cap_drop ALL, read-only rootfs, no network) still hold.
        self.container.remove(force=True)
        self.container = None
        if not (no_new_privileges and "operation not permitted" in why.lower()):
            raise SandboxError(f"sandbox {self.id} exited immediately: {why}")
        log.warning(
            "sandbox_no_new_privileges_unsupported",
            sandbox=self.id,
            detail=why,
            note="retrying without no_new_privs; other sandbox boundaries are unchanged",
        )
        self._start_sync(no_new_privileges=False)

    def _exit_reason(self) -> str | None:
        """None once the container is *stably* running, else why it stopped.

        Docker reports ``running`` the moment it hands off to the runtime, before the
        entrypoint has actually exec'd, so a single check can pass for a container that
        dies milliseconds later. Require several consecutive running samples.
        """
        assert self.container is not None
        deadline = time.monotonic() + START_TIMEOUT_S
        consecutive_running = 0
        while time.monotonic() < deadline:
            self.container.reload()
            status = self.container.status
            if status in ("exited", "dead"):
                logs = self.container.logs(tail=5).decode(errors="replace").strip()
                state = self.container.attrs.get("State", {})
                return logs or state.get("Error") or f"exit code {state.get('ExitCode')}"
            consecutive_running = consecutive_running + 1 if status == "running" else 0
            if consecutive_running >= START_SETTLE_SAMPLES:
                return None
            time.sleep(START_POLL_S)
        return f"container never reached a stable running state (last status: {status})"

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

    async def kill_exec(self) -> None:
        """Kill the container so a running command stops now. See Sandbox.kill_exec.

        An in-flight ``exec`` unblocks by itself: docker reports the exec's own status, so
        the waiting call returns 137 (128 + SIGKILL) within about a second. The
        ``APIError`` branch in ``exec`` is the fallback for the case where the container is
        gone entirely rather than merely killed.

        The container is left in place rather than removed, so teardown can still keep it
        for inspection if the run asked for that.
        """
        if self.container is None:
            return
        container = self.container

        def _kill() -> None:
            with contextlib.suppress(NotFound, APIError):
                container.kill()

        await asyncio.to_thread(_kill)
        log.info("sandbox_exec_killed", sandbox=self.id)

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
        # The span wraps the whole call rather than just the subprocess wait, so the
        # attributes below land on it. Annotating outside it would put the command and the
        # exit code on the parent span, where they would read as the *tool's* exit code.
        with trace_span("sandbox.exec", sandbox=self.id, command=cmd[:200], timeout_s=timeout_s):
            return await self._exec_inner(
                cmd, timeout_s=timeout_s, cwd=cwd, env=env, max_output_bytes=max_output_bytes
            )

    async def _exec_inner(
        self,
        cmd: str,
        *,
        timeout_s: int,
        cwd: str,
        env: dict[str, str] | None,
        max_output_bytes: int,
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
        except (APIError, NotFound) as e:
            # The container went away under us. The usual cause is `kill_exec` — a human
            # cancelled — and a killed command is a failed command, not a crashed
            # orchestrator. The cancel itself is what ends the run, on the next check.
            log.info("sandbox_exec_interrupted", sandbox=self.id, detail=str(e)[:200])
            return ExecResult(
                exit_code=EXIT_KILLED,
                stdout="",
                stderr="the sandbox was stopped while this command was running",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        duration_ms = int((time.monotonic() - t0) * 1000)
        stdout, t1 = cap_output(out, max_output_bytes)
        stderr, t2 = cap_output(err, max_output_bytes)
        annotate(exit_code=code, truncated=t1 or t2, timed_out=code == EXIT_TIMEOUT)
        metrics.record_sandbox_exec(duration_ms / 1000)
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
