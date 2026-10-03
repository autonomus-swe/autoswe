"""Egress control for the dependency install.

The install window is the one time a sandbox has a network, and until now it had the whole
internet. This confines it to an allow-list — but the interesting part is *what does the
confining*, and it is not the allow-list.

**The proxy alone is theatre.** On a normal bridge network a container has a default route
and reaches anything it likes, ignoring `HTTP_PROXY` entirely. The enforcement is the
network being `internal`: no default route, no DNS, nothing reachable but the proxy. The
first test below is the control that proves it, and it is the reason the code treats an
existing non-internal network as a hard failure rather than a warning.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from core.errors import SandboxError
from sandbox.docker import DockerSandbox

pytestmark = pytest.mark.integration

IMAGE = "agent-sandbox:python-3.12"
PROXY_IMAGE = "agent-egress-proxy"


def _docker_ready(*images: str) -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        for image in images:
            client.images.get(image)
        return True
    except Exception:
        return False


requires_sandbox = pytest.mark.skipif(
    not _docker_ready(IMAGE), reason=f"docker or {IMAGE} unavailable (run `make sandbox-image`)"
)
requires_proxy = pytest.mark.skipif(
    not _docker_ready(IMAGE, PROXY_IMAGE),
    reason=f"{PROXY_IMAGE} unavailable (run `make egress-proxy-image`)",
)


MakeNetwork = Callable[..., Any]


@pytest.fixture
def networks() -> Iterator[MakeNetwork]:
    """Two throwaway networks, one internal and one not, removed however the test ends."""
    import docker

    client = docker.from_env()
    made = []

    def make(internal: bool) -> Any:
        net = client.networks.create(f"egress-test-{uuid.uuid4().hex[:8]}", internal=internal)
        made.append(net)
        return net

    try:
        yield make
    finally:
        for net in made:
            try:
                net.remove()
            except Exception:  # a container may still be detaching
                pass


def has_default_route(proc_net_route: str) -> bool:
    """Whether `/proc/net/route` holds a default route.

    The DESTINATION column, not the gateway. An on-link subnet route has a gateway of
    `00000000` too — `eth0 000014AC 00000000 0001` is the internal network's own subnet —
    so looking for that string anywhere in the line reports "no default route" about a
    network that has one. That is the wrong answer in the direction that would let this
    whole file pass while enforcing nothing, which is how the first version of this test
    failed.
    """
    for line in proc_net_route.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000":
            return True
    return False


def box(workspace: Path, network: str, *, internal: bool) -> DockerSandbox:
    return DockerSandbox(
        uuid.uuid4(),
        workspace,
        image=IMAGE,
        network=network,
        user=f"{os.getuid()}:{os.getgid()}",
        network_internal=internal,
    )


# ---- what actually does the confining -------------------------------------------------------


@requires_sandbox
async def test_an_internal_network_has_no_way_out_at_all(
    host_tmp: Path, networks: MakeNetwork
) -> None:
    """No default route, and no DNS either — so neither a direct connection nor a DNS
    tunnel is available, whatever the proxy allows."""
    ws = host_tmp / "internal"
    ws.mkdir()
    sb = box(ws, str(networks(internal=True).name), internal=True)
    await sb.start()
    try:
        await sb.connect_install_network()
        routes = await sb.exec("cat /proc/net/route")
        direct = await sb.exec("timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' 2>&1 || true")
        dns = await sb.exec("getent hosts pypi.org; echo exit=$?")
    finally:
        await sb.stop(remove=True)

    assert not has_default_route(routes.stdout), "an internal network has no default route"
    assert "unreachable" in direct.stdout.lower() or direct.stdout.strip() == ""
    assert "exit=2" in dns.stdout, "DNS is dead too, so a DNS tunnel is not an escape either"


@requires_sandbox
async def test_a_normal_network_reaches_the_internet_directly(
    host_tmp: Path, networks: MakeNetwork
) -> None:
    """The control, and the reason the network flag is not optional. This is what every
    machine that has ever run autoswe has today: an `agent-install` bridge with a default
    route, where a proxy allow-list would be advice rather than a boundary."""
    ws = host_tmp / "open"
    ws.mkdir()
    sb = box(ws, str(networks(internal=False).name), internal=False)
    await sb.start()
    try:
        await sb.connect_install_network()
        routes = await sb.exec("cat /proc/net/route")
    finally:
        await sb.stop(remove=True)

    assert has_default_route(routes.stdout), (
        "a plain bridge has a default route; the proxy is not the only way out"
    )


@requires_sandbox
async def test_an_existing_open_network_is_refused_rather_than_reused(
    host_tmp: Path, networks: MakeNetwork
) -> None:
    """The failure this guards is the worst possible shape: the deny tests still pass,
    because the proxy really does return 403 — it just is not the only way out.

    `networks.get` hands back whatever exists, `create` on a live name is a 409, and the
    network cannot be removed while a run holds an endpoint. So silent reuse would leave
    every machine that has run autoswe before with unrestricted egress and a green suite.
    """
    ws = host_tmp / "mismatch"
    ws.mkdir()
    open_network = str(networks(internal=False).name)

    with pytest.raises(SandboxError, match="not internal"):
        await box(ws, open_network, internal=True).start()


# ---- the allow-list ---------------------------------------------------------------------------


@requires_proxy
async def test_an_allow_listed_host_is_reachable_and_others_are_not(
    host_tmp: Path, networks: MakeNetwork
) -> None:
    """Through the proxy, on the internal network, with nothing else available."""
    import docker

    client = docker.from_env()
    net = networks(internal=True)
    proxy = client.containers.run(
        PROXY_IMAGE,
        name=f"egress-proxy-{uuid.uuid4().hex[:8]}",
        detach=True,
        network=net.name,
        read_only=True,
        tmpfs={"/tmp": ""},
        cap_drop=["ALL"],
    )
    # The proxy needs a way out itself; the sandbox side stays internal.
    client.networks.get("bridge").connect(proxy)

    ws = host_tmp / "allowed"
    ws.mkdir()
    sb = box(ws, str(net.name), internal=True)
    await sb.start()
    try:
        await sb.connect_install_network()
        proxy.reload()
        url = f"http://{proxy.name}:8888"
        env = {"HTTPS_PROXY": url, "https_proxy": url, "HTTP_PROXY": url, "http_proxy": url}
        # `/simple/pip/` rather than `/simple/`, which is the *whole* package index — 46 MB.
        # What is under test is the proxy's allow-list decision, and for an HTTPS request
        # that is made at CONNECT, before any of the body moves; downloading the index
        # proves nothing extra and makes the test hostage to bandwidth. Measured on a slow
        # link: 188 s for `/simple/` against a 120 s exec timeout, so the request was killed
        # and `%{http_code}` came back empty — a red test about egress policy, caused by
        # throughput. `/simple/pip/` is 107 KB of real content through the same proxy, in
        # 0.07 s.
        allowed = await sb.exec(
            "curl -sS -o /dev/null -w '%{http_code}' --max-time 60 https://pypi.org/simple/pip/",
            env=env,
        )
        denied = await sb.exec("curl -sS https://example.com 2>&1 || true", env=env)
        unproxied = await sb.exec("curl -sS --max-time 8 https://pypi.org 2>&1 || true")
    finally:
        await sb.stop(remove=True)
        proxy.remove(force=True)

    assert allowed.stdout.strip() == "200", allowed.stdout
    assert "403" in denied.stdout, denied.stdout
    assert "200" not in unproxied.stdout, "without the proxy there is no route at all"
