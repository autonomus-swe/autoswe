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
