"""Docker sandbox contract (docs/PHASE-1 Step 1.1). Skipped when Docker or the image is absent."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from core.errors import SandboxError
from sandbox.base import TRUNCATION_MARKER, cap_output
from sandbox.docker import DockerSandbox

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


def test_cap_output_keeps_head_and_tail() -> None:
    text, truncated = cap_output(b"a" * 1000 + b"b" * 1000, 100)
    assert truncated and text.startswith("aaaaa") and text.endswith("bbbbb")
    assert TRUNCATION_MARKER.format(n=1900) in text
    assert cap_output(b"short", 100) == ("short", False)


@requires_docker
async def test_exec_basic(box: DockerSandbox) -> None:
    res = await box.exec("echo hi")
    assert res.exit_code == 0 and res.stdout == "hi\n" and res.ok


@requires_docker
async def test_exec_timeout_is_enforced(box: DockerSandbox) -> None:
    t0 = time.monotonic()
    res = await box.exec("sleep 30", timeout_s=2)
    assert res.timed_out and not res.ok
    assert time.monotonic() - t0 < 12


@requires_docker
async def test_rootfs_read_only_but_workspace_writable(box: DockerSandbox) -> None:
    assert (await box.exec("touch /etc/x")).exit_code != 0
    assert (await box.exec("touch /workspace/x")).exit_code == 0
    assert (box.workspace / "x").exists()
    assert (await box.exec("touch /tmp/y")).exit_code == 0


@requires_docker
async def test_network_is_gone_after_disconnect(box: DockerSandbox) -> None:
    assert await box.has_network()
    await box.disconnect_network()
    assert not await box.has_network()
    assert (await box.exec("getent hosts pypi.org")).exit_code != 0
    routes = await box.exec("ip route 2>/dev/null || cat /proc/net/route | tail -n +2")
    assert routes.stdout.strip() == ""


@requires_docker
async def test_no_host_secrets_in_env(box: DockerSandbox) -> None:
    out = (await box.exec("env")).stdout
    for marker in ("ANTHROPIC", "GITHUB", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY"):
        assert marker not in out


@requires_docker
async def test_large_output_is_capped(box: DockerSandbox) -> None:
    res = await box.exec("python -c \"print('x'*100000)\"")
    assert res.truncated and len(res.stdout) < 41_000 and "[truncated" in res.stdout


@requires_docker
async def test_runs_as_configured_user_with_no_capabilities(box: DockerSandbox) -> None:
    assert (await box.exec("id -u")).stdout.strip() == str(os.getuid())
    caps = (await box.exec("grep CapEff /proc/self/status")).stdout.split()[-1]
    assert int(caps, 16) == 0
    # no_new_privs is requested; some Docker builds refuse it and the sandbox falls back.
    nnp = (await box.exec("grep NoNewPrivs /proc/self/status")).stdout.split()[-1]
    assert (nnp == "1") == box.effective_no_new_privileges


@requires_docker
async def test_start_fails_loudly_when_the_container_cannot_run(host_tmp: Path) -> None:
    ws = host_tmp / "bad"
    ws.mkdir()
    sb = DockerSandbox(
        uuid.uuid4(),
        ws,
        image=IMAGE,
        network="agent-install",
        user="4000:4000",
        no_new_privileges=False,
    )
    sb._start_sync = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        SandboxError("boom")
    )
    with pytest.raises(SandboxError):
        await sb.start()


@requires_docker
async def test_stop_is_idempotent(host_tmp: Path) -> None:
    ws = host_tmp / "ws2"
    ws.mkdir()
    sb = DockerSandbox(
        uuid.uuid4(), ws, image=IMAGE, network="agent-install", user=f"{os.getuid()}:{os.getgid()}"
    )
    await sb.start()
    name = sb.id
    await sb.stop()
    await sb.stop()
    import docker
    from docker.errors import NotFound

    with pytest.raises(NotFound):
        docker.from_env().containers.get(name)


# ---- resource limits -----------------------------------------------------------------------
#
# The phase document says a container's limits "cannot be recreated", so the higher value
# for a compiling install must be predicted at creation. Measured here, that is wrong: both
# dials move on a running container. These tests exist to keep that true — if a future
# change goes back to `nano_cpus`, creation still works and only `set_cpus` silently stops
# doing anything, which no other test would notice.


@requires_docker
async def test_the_requested_limits_are_what_the_cgroup_reports(host_tmp: Path) -> None:
    """`cpu_quota`/`cpu_period` rather than `nano_cpus`. Same cgroup value — that is the
    point — but only these two are accepted by `container.update()`."""
    ws = host_tmp / "limits"
    ws.mkdir()
    sb = DockerSandbox(
        uuid.uuid4(),
        ws,
        image=IMAGE,
        network="agent-install",
        user=f"{os.getuid()}:{os.getgid()}",
        mem_limit="6g",
        cpus=2.0,
    )
    await sb.start()
    try:
        res = await sb.exec("cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max")
    finally:
        await sb.stop(remove=True)

    cpu_max, cpu_period, memory_max = res.stdout.split()
    assert (cpu_max, cpu_period) == ("200000", "100000"), "2 CPUs, as nano_cpus=2e9 also gave"
    assert int(memory_max) == 6 * 1024**3


@requires_docker
async def test_cpus_can_be_raised_for_an_install_and_handed_back(box: DockerSandbox) -> None:
    """The install is the only CPU-bound part of a run — a Go build, a node-gyp build — and
    the only part with a network. Everything after is waiting on a model."""
    before = (await box.exec("cat /sys/fs/cgroup/cpu.max")).stdout.strip()

    await box.set_cpus(4.0)
    raised = (await box.exec("cat /sys/fs/cgroup/cpu.max")).stdout.strip()
    await box.set_cpus(2.0)
    restored = (await box.exec("cat /sys/fs/cgroup/cpu.max")).stdout.strip()

    assert before == "200000 100000"
    assert raised == "400000 100000", "the running container was changed, not recreated"
    assert restored == before


@requires_docker
async def test_asking_for_more_cpus_than_the_host_has_is_clamped(box: DockerSandbox) -> None:
    """Docker validates a CPU limit against the host's core count and answers 400 —
    "range of CPUs is from 0.01 to 12.00" on this machine. Unclamped, a limit chosen on a
    big machine would kill the run in SETUP on every smaller one, which is most CI."""
    host = box._host_cpus()
    assert host > 0, "the daemon did not report a CPU count"

    assert box._quota(host * 100) == host * 100_000
    assert box._quota(1.0) == 100_000, "a request under the ceiling is left alone"


@requires_docker
async def test_a_nonsense_cpu_count_is_clamped_rather_than_sent(box: DockerSandbox) -> None:
    """`set_cpus` cannot construct an invalid request: the floor in `_quota` means even a
    negative or absurdly small number becomes a legal quota. Worth pinning, because the
    obvious way to test the error path — passing a bad number — silently tests nothing."""
    await box.set_cpus(-5.0)

    cpu_max = (await box.exec("cat /sys/fs/cgroup/cpu.max")).stdout.strip()
    assert cpu_max == "1000 100000", "the 0.01 floor, not an error and not unchanged"


async def test_a_limit_change_on_a_container_that_is_gone_does_not_raise(
    host_tmp: Path,
) -> None:
    """The real failure mode: the install dies because the container went away, and the
    `finally` that hands the CPUs back runs against nothing. Losing the run there would
    turn a slow install into a failed one."""
    ws = host_tmp / "gone"
    ws.mkdir()
    sb = DockerSandbox(
        uuid.uuid4(),
        ws,
        image=IMAGE,
        network="agent-install",
        user=f"{os.getuid()}:{os.getgid()}",
    )
    await sb.start()
    await sb.stop(remove=True)

    await sb.set_cpus(2.0)  # must not raise
