"""The isolation flags, asserted where they actually leave the process.

`docs/sandbox-hardening.md` and the Phase 5 deliverables both promise a specific posture:
non-root, read-only rootfs, every capability dropped, a pids ceiling, and an optional
gVisor runtime. Each of those is one keyword argument on one `containers.run` call, and a
keyword argument that stops being passed fails silently — the container still starts, the
run still succeeds, and the only thing that changes is that the isolation is gone.

The gVisor flag is the sharpest case. `sandbox_runtime` threads from settings through
`Deps` into `DockerSandbox.runtime` and out as `kwargs["runtime"]`, and nothing tested any
hop of it. An operator who sets `SANDBOX_RUNTIME=runsc` believes they have moved the
workload onto a user-space kernel; if the value were dropped anywhere along the way they
would get runc and no error. This repository has already shipped exactly that bug once, in
`Route.tier` — a field carried carefully to a provider that never read it, so the budget
downgrade was decorative for its whole life.

So these assert against the arguments Docker is handed, using a fake client, rather than
against the attributes the object stores. Storing `self.runtime` correctly and never
passing it is the failure being tested for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from docker.errors import NotFound

from sandbox.docker import DockerSandbox

pytestmark = pytest.mark.unit


class FakeContainer:
    """Running, immediately and stably — `_exit_reason` samples this several times."""

    status = "running"

    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {"State": {}}

    def reload(self) -> None:
        return None

    def logs(self, tail: int = 5) -> bytes:
        return b""

    def exec_run(self, argv: list[str], **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        """`start()` prepares /tmp before it returns; succeed so startup completes."""
        return 0, (b"", b"")


class FakeContainers:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.image: str | None = None

    def run(self, image: str, **kwargs: Any) -> FakeContainer:
        self.image, self.kwargs = image, dict(kwargs)
        return FakeContainer()

    def get(self, name: str) -> FakeContainer:
        raise NotFound(name)  # no crashed earlier run left a container with our name


class FakeNetworks:
    """Absent, so `_ensure_network` takes its create path.

    `NotFound` specifically, not any exception: that is what the Docker SDK raises and what
    the code catches, and a fake that raises something else would be testing a code path
    the daemon never produces.
    """

    def __init__(self) -> None:
        self.created: list[tuple[str, bool]] = []

    def get(self, name: str) -> Any:
        raise NotFound(name)

    def create(self, name: str, *, driver: str = "bridge", internal: bool = False) -> Any:
        self.created.append((name, internal))
        return None


class FakeClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.networks = FakeNetworks()

    def info(self) -> dict[str, Any]:
        return {"NCPU": 8}


async def started(run_id: Any = None, **overrides: Any) -> tuple[DockerSandbox, FakeContainers]:
    """Start a sandbox against a fake daemon and hand back what Docker was asked for."""
    client = FakeClient()
    options: dict[str, Any] = {
        "image": "agent-sandbox:python-3.12",
        "network": "none",
        "user": "1000:1000",
        "client": client,
    }
    options.update(overrides)
    sandbox = DockerSandbox(run_id or uuid4(), Path("/tmp"), **options)
    await sandbox.start()
    return sandbox, client.containers


# ---- the gVisor flag ----------------------------------------------------------------------


async def test_the_gvisor_runtime_reaches_docker() -> None:
    """The hop nothing tested. `SANDBOX_RUNTIME=runsc` has to arrive as `runtime=runsc`."""
    _, containers = await started(runtime="runsc")

    assert containers.kwargs.get("runtime") == "runsc"


async def test_no_runtime_configured_sends_no_runtime_key() -> None:
    """Not `runtime=None`: the Docker SDK forwards an explicit null and some daemons
    reject it, so the default deployment must not send the key at all."""
    _, containers = await started()

    assert "runtime" not in containers.kwargs


# ---- the posture that is on by default ------------------------------------------------------


async def test_the_container_is_not_root_and_cannot_write_its_own_filesystem() -> None:
    _, containers = await started()

    assert containers.kwargs["user"] == "1000:1000"
    assert containers.kwargs["read_only"] is True


async def test_every_capability_is_dropped_and_processes_are_capped() -> None:
    """`cap_drop=["ALL"]` is the difference between a compromised agent being a nuisance
    and it being root on the host's kernel interfaces."""
    _, containers = await started()

    assert containers.kwargs["cap_drop"] == ["ALL"]
    assert containers.kwargs["pids_limit"] == 512


async def test_new_privileges_are_refused_by_default() -> None:
    _, containers = await started()

    assert containers.kwargs["security_opt"] == ["no-new-privileges:true"]


async def test_the_only_writable_place_is_a_bounded_tmpfs() -> None:
    """A read-only rootfs with an unbounded writable mount is a read-write container with
    extra steps: `/tmp` has to carry a size, and it is charged against `mem_limit`."""
    _, containers = await started(tmpfs_size="256m")

    assert containers.kwargs["tmpfs"] == {"/tmp": "size=256m,exec"}


async def test_the_memory_and_cpu_ceilings_are_the_ones_asked_for() -> None:
    """`cpu_quota`/`cpu_period` rather than `nano_cpus` — the same cgroup value, but only
    these two can be changed afterwards by `container.update()` for the install bump."""
    _, containers = await started(mem_limit="2g", cpus=1.5)

    assert containers.kwargs["mem_limit"] == "2g"
    assert containers.kwargs["cpu_quota"] / containers.kwargs["cpu_period"] == pytest.approx(1.5)


async def test_the_run_is_labelled_with_its_own_id_so_the_collector_can_find_it() -> None:
    """`orchestrator/gc.py` sweeps on this label. A container carrying no label, or another
    run's label, is one that leaks when a worker dies mid-run."""
    run_id = uuid4()

    sandbox, containers = await started(run_id)

    assert containers.kwargs["labels"] == {"autoswe.run_id": str(run_id)}
    assert sandbox.id == f"run-{run_id}"
    assert containers.kwargs["name"] == sandbox.id
