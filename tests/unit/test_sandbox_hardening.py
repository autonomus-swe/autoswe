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


# ---- the environment the container is handed ------------------------------------------------
#
# `docs/security.md` §7 claims "the sandbox never receives API keys or database credentials"
# and cites this file as the proof. Until now this file never read `kwargs["environment"]`
# at all: every assertion above is about some other keyword argument, and `FakeContainers.run`
# swallows whatever else it is passed. Spreading the host's `os.environ` into that dict
# survived the whole suite — the agent would have been handed the orchestrator's model keys
# and its database URL, in a container it is allowed to run arbitrary commands in.

# Written out rather than imported from `sandbox.docker`, because importing the constant
# would assert the source against itself. These five exist so `uv` can run offline as a
# non-root uid with nothing writable but the tmpfs.
SANDBOX_ENV: dict[str, str] = {
    "HOME": "/tmp/home",
    "UV_CACHE_DIR": "/tmp/uv",
    "UV_PYTHON_DOWNLOADS": "never",
    "UV_LINK_MODE": "copy",
    "PIP_NO_CACHE_DIR": "1",
}

# Names an orchestrator really does hold, with values no sandbox default could coincide
# with, so a leak is unmistakable in the failure output rather than plausible.
HOST_SECRETS: dict[str, str] = {
    "ANTHROPIC_API_KEY": "sk-ant-this-must-not-reach-the-sandbox-4f21",
    "DATABASE_URL": "postgresql://leak:leak@db.invalid:5432/leak",
    "AWS_SECRET_ACCESS_KEY": "wJalrNeverLeavesTheHost7Qe",
}


async def test_the_host_environment_does_not_reach_the_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compromised agent reading its own `/proc/self/environ` must find nothing it can spend.

    The sandbox runs text written by a repository against a model that can be talked into
    anything, so the environment it is handed is a list of what an attacker gets for free.
    An explicit dict gives them nothing; the host's own environment gives them this
    deployment's model key, its database credentials and its cloud credentials at once, with
    a shell already available to use them.

    Asserted on the *key set*, not on values, and that is the point of the test. The leak
    that survived spread `os.environ` ahead of the explicit entries, so `HOME` and
    `UV_CACHE_DIR` still came out as the sandbox's own — every value assertion anyone would
    naturally write stays green while the key set quietly triples.
    """
    for name, value in HOST_SECRETS.items():
        monkeypatch.setenv(name, value)

    _, containers = await started()
    environment: dict[str, str] = containers.kwargs["environment"]

    assert set(environment) == set(SANDBOX_ENV), (
        "the sandbox's environment is an explicit allow-list; it gained or lost "
        f"{sorted(set(environment) ^ set(SANDBOX_ENV))}. If a variable is genuinely needed "
        "inside the container, add it here too and say why in docs/security.md §7"
    )
    leaked = sorted(k for k, v in environment.items() if v in set(HOST_SECRETS.values()))
    assert leaked == [], f"host credentials reached the sandbox under the names {leaked}"


async def test_the_container_still_gets_the_variables_uv_needs_to_run_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterweight: `environment={}` passes the leak test above and breaks every run.

    Without `HOME` and `UV_CACHE_DIR` pointed at the tmpfs, `uv` writes to a rootfs that is
    read-only and the dependency install dies in SETUP; without `UV_PYTHON_DOWNLOADS=never`
    it tries to fetch an interpreter after the network has been taken away. So the claim
    being proved is two-sided — nothing of the host's, and all of the sandbox's.

    The host values here deliberately collide with the sandbox's own names. That catches the
    other half of the leak: spreading `os.environ` *after* the explicit entries rather than
    before would point `HOME` at a host directory the container cannot even see.
    """
    monkeypatch.setenv("HOME", "/home/orchestrator-not-the-sandbox")
    monkeypatch.setenv("UV_CACHE_DIR", "/var/cache/host-uv")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "automatic")

    _, containers = await started()

    assert containers.kwargs["environment"] == SANDBOX_ENV, (
        "the container's environment is not the dict the sandbox chose, so either a variable "
        "uv needs went missing or the host's copy of one displaced it"
    )


# ---- the install network --------------------------------------------------------------------
#
# `docs/security.md` §2 claims "an internal network has no route out", and cites an
# integration test that builds the network itself in its fixture and hands the name in. That
# makes `_ensure_network` take its `networks.get` branch every time, so the branch that
# actually creates the network has never run under assertion. Creating it as a plain bridge
# survived: the sandbox reaches the open internet directly and ignores the egress proxy, while
# every allow-list test still passes, because the proxy does return 403 — it just stops being
# the only way out.
#
# `FakeNetworks.get` above raises `NotFound` unconditionally, so these take the create path.


async def created_networks(**overrides: Any) -> list[tuple[str, bool]]:
    """Start a sandbox against a fresh fake daemon and hand back the networks it created.

    `started` returns the containers fake only, and the record the create path leaves behind
    is on the networks fake.
    """
    client = FakeClient()
    await started(client=client, **overrides)
    return client.networks.created


async def test_the_install_network_is_created_internal_when_egress_is_enforced() -> None:
    """An install network created as a plain bridge is an egress allow-list with no teeth.

    `EGRESS_ENFORCED=true` is an operator saying the sandbox may reach exactly the hosts on
    the list. `internal` is what makes that true: without a default route the only way off
    the subnet is the proxy. Create the same network as an ordinary bridge and the container
    talks to the internet directly, the proxy is never consulted, and nothing anywhere
    reports a difference.
    """
    created = await created_networks(network="autoswe-install-enforced", network_internal=True)

    assert created == [("autoswe-install-enforced", True)], (
        "the install network was not created internal, so the sandbox has a route around "
        "the egress proxy"
    )


async def test_a_sandbox_that_was_not_asked_for_an_internal_network_does_not_get_one() -> None:
    """The counterweight: the flag has to be threaded, not hard-coded either way.

    Hard-coding `internal=True` would satisfy the test above and break every default
    deployment instead — an internal network has no route out for `uv sync` either, so
    dependency installation fails in SETUP on a machine nobody asked to lock down.
    """
    created = await created_networks(network="autoswe-install-open")

    assert created == [("autoswe-install-open", False)], (
        "a deployment that did not enable egress enforcement got an internal network, which "
        "leaves the dependency install with nowhere to fetch from"
    )
