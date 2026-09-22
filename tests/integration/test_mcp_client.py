"""Mounting a real external MCP server, as a subprocess, and calling it.

The stub in `tests/fixtures/stub_mcp_server.py` is a separate process speaking MCP over
its pipes, because that is what a mounted server is. An in-process stub would skip the
half of `mcp_bridge/client.py` that is about processes: the environment the child
receives, a session outliving the task that made a call, a server that will not start.

The criterion this file exists for has two halves — a read-only tool an Analyzer can call,
and a mutating one that pauses the run for approval — and the second is checked through
`OrchestratorHooks`, not by reading `requires_approval` off the tool. A flag nothing
consults is not a gate.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from contracts import Budget
from mcp_bridge.client import MCPServers
from mcp_bridge.config import ServerConfig
from orchestrator.approvals import ApprovalGate
from orchestrator.hooks import OrchestratorHooks
from storage import repo as db
from storage.db import session
from storage.redis import RedisBus
from tools import registry

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
STUB = str(REPO_ROOT / "tests" / "fixtures" / "stub_mcp_server.py")
ALLOWED = ["echo", "write", "leaks", "explode"]


def stub_config(**overrides: Any) -> ServerConfig:
    """The stub, mounted the way `mcp_servers.yaml` would mount a real server."""
    fields: dict[str, Any] = {
        "name": "stub",
        "command": [sys.executable, STUB],
        # Only what is named here reaches the child, plus the SDK's own safe-list.
        "env": {"PYTHONPATH": str(REPO_ROOT), "STUB_SECRET": "named-on-purpose"},
        "roles": ["coder"],
        "allow": list(ALLOWED),
        "mutating": ["write"],
        "timeout_s": 30,
    }
    fields.update(overrides)
    return ServerConfig(**fields)


@asynccontextmanager
async def mounted(*configs: ServerConfig) -> AsyncIterator[tuple[MCPServers, list[Any]]]:
    """Started servers and their tools, closed on the way out.

    A context manager rather than a fixture: the sessions hold anyio cancel scopes, and a
    yielding async fixture is resumed in a different task at teardown.
    """
    servers = MCPServers(list(configs))
    tools = await servers.start()
    try:
        yield servers, tools
    finally:
        await servers.aclose()


@pytest.fixture
def clean_registry() -> Iterator[None]:
    """The registry is module state, and these tests add to it."""
    tools = dict(registry.REGISTRY)
    roles = {role: list(names) for role, names in registry.ROLE_TOOLS.items()}
    yield
    registry.REGISTRY.clear()
    registry.REGISTRY.update(tools)
    for role, names in roles.items():
        registry.ROLE_TOOLS[role][:] = names


# ---- mounting ---------------------------------------------------------------------------


async def test_only_allow_listed_tools_are_mounted() -> None:
    """The stub offers `not_allowed` as well. The allow-list is the boundary, so a tool
    the server offers and the configuration does not name never becomes callable."""
    async with mounted(stub_config()) as (_, tools):
        names = {tool.name for tool, _ in tools}
    assert names == {"mcp_stub_echo", "mcp_stub_write", "mcp_stub_leaks", "mcp_stub_explode"}
    assert "mcp_stub_not_allowed" not in names


async def test_a_tool_carries_the_servers_schema_and_the_files_policy() -> None:
    """Two different sources on purpose. The schema describes what the tool accepts, which
    only the server knows; `mutating` says what it is permitted to do, which the server
    does not get a vote on — otherwise it could disarm its own approval gate."""
    async with mounted(stub_config()) as (_, tools):
        by_name = {tool.name: tool for tool, _ in tools}

    write = by_name["mcp_stub_write"]
    assert sorted(write.input_schema["properties"]) == ["content", "path"]
    assert write.mutating and write.requires_approval and not write.parallel_safe

    echo = by_name["mcp_stub_echo"]
    assert sorted(echo.input_schema["properties"]) == ["text"]
    assert not echo.mutating and not echo.requires_approval and echo.parallel_safe


async def test_the_child_gets_only_the_environment_it_was_given() -> None:
    """A mounted server is a subprocess. Started with the worker's environment it would
    hold the model key, the database DSN and the GitHub token — and it is somebody else's
    code, chosen from a registry, reading a repository the run does not control."""
    async with mounted(stub_config()) as (servers, _):
        result = await servers.call("stub", "leaks", {})
    received = set(result.content.split(","))
    assert "STUB_SECRET" in received  # what we named arrives
    assert not received & {"ANTHROPIC_API_KEY", "DATABASE_URL", "GITHUB_TOKEN", "LLM_API_KEY"}


async def test_a_server_that_will_not_start_costs_its_tools_and_nothing_else() -> None:
    """An unreachable GitHub server is a reason to run without GitHub tools, not a reason
    to refuse every run."""
    broken = stub_config(name="broken", env={"STUB_REFUSE_TO_START": "1"})
    async with mounted(broken, stub_config()) as (_, tools):
        names = {tool.name for tool, _ in tools}
    assert not any(n.startswith("mcp_broken_") for n in names)
    assert "mcp_stub_echo" in names


async def test_a_name_the_server_does_not_offer_is_skipped_not_invented() -> None:
    async with mounted(stub_config(allow=[*ALLOWED, "ghost"])) as (_, tools):
        names = {tool.name for tool, _ in tools}
    assert "mcp_stub_ghost" not in names
    assert "mcp_stub_echo" in names


# ---- calling ----------------------------------------------------------------------------


async def test_a_read_only_tool_returns_what_the_server_returned() -> None:
    async with mounted(stub_config()) as (servers, _):
        result = await servers.call("stub", "echo", {"text": "hello"})
    assert not result.is_error and result.content == "echo: hello"


async def test_a_failing_remote_tool_is_an_error_result_not_a_crashed_step() -> None:
    """The model gets to read it and try something else, which is what every other tool
    failure does."""
    async with mounted(stub_config()) as (servers, _):
        result = await servers.call("stub", "explode", {})
    assert result.is_error and "explode" in result.content


async def test_a_tool_outside_the_allow_list_is_refused_at_the_client_too() -> None:
    """Unreachable through the mounted tools, which are built from the allow-list. Checked
    again because the allow-list is the security boundary, and a boundary enforced in one
    place moves the next time that place is refactored."""
    async with mounted(stub_config()) as (servers, _):
        result = await servers.call("stub", "not_allowed", {})
    assert result.is_error and "not allowed" in result.content


async def test_the_session_is_shared_across_calls_and_survives_them() -> None:
    """One subprocess per server per process, not one per call. Starting
    `npx @modelcontextprotocol/server-github` for every tool call would add seconds to
    each and leave a process behind whenever a run was cancelled."""
    async with mounted(stub_config()) as (servers, _):
        first = await servers.call("stub", "echo", {"text": "one"})
        second = await servers.call("stub", "echo", {"text": "two"})
        assert len(servers._connections) == 1
    assert first.content == "echo: one" and second.content == "echo: two"


async def test_calls_from_separate_tasks_share_one_session() -> None:
    """A session is entered by its supervisor task and used from whichever task is running
    a tool. Both halves matter: the cancel scope stays with its owner, and a run's calls do
    not each need their own subprocess."""
    async with mounted(stub_config()) as (servers, _):
        results = await asyncio.gather(
            *(servers.call("stub", "echo", {"text": str(i)}) for i in range(4))
        )
        assert len(servers._connections) == 1
    assert [r.content for r in results] == [f"echo: {i}" for i in range(4)]


async def test_a_dead_server_is_reconnected_rather_than_left_broken() -> None:
    """A worker lives for days and a child process does not have to.

    This caught a real one. A connection whose session had died stayed in the map with its
    `ready` future already resolved, so every later call found it, found no client, and
    failed identically — a worker that needed restarting because a child had crashed once.
    """
    async with mounted(stub_config()) as (servers, _):
        assert (await servers.call("stub", "echo", {"text": "before"})).content == "echo: before"
        first = servers._connections["stub"]

        # Kill the session the way a crashed child would: the supervisor returns and the
        # connection is left registered and unusable.
        first.stop.set()
        await asyncio.wait_for(asyncio.shield(first.task), timeout=10)
        assert first.dead

        after = await servers.call("stub", "echo", {"text": "after"})
        assert servers._connections["stub"] is not first  # a new child, not the corpse
    assert not after.is_error and after.content == "echo: after"


# ---- the gates ---------------------------------------------------------------------------


async def test_mounted_tools_reach_the_registry_and_their_roles(clean_registry: None) -> None:
    async with mounted(stub_config()) as (_, tools):
        registry.register(tools)
        assert "mcp_stub_echo" in [t.name for t in registry.tools_for("coder")]
        assert "mcp_stub_echo" not in [t.name for t in registry.tools_for("analyzer")]
        assert registry.REGISTRY["mcp_stub_write"].requires_approval


async def make_run(engine: AsyncEngine) -> uuid.UUID:
    async with session(engine) as s:
        run_id = await db.create_run(
            s,
            repo_url="https://github.com/acme/demo",
            base_branch="main",
            goal="do the thing",
            budget=Budget(),
        )
        await db.set_run_phase(s, run_id, "code", "running")
    return run_id


def hooks_for(run_id: uuid.UUID, engine: AsyncEngine, bus: RedisBus) -> OrchestratorHooks:
    return OrchestratorHooks(
        run_id=run_id,
        step_id=uuid.uuid4(),
        engine=engine,
        bus=bus,
        provider_name="test",
        model="test",
        effort=None,
        role="coder",
        approvals=ApprovalGate(run_id=run_id, bus=bus, engine=engine),
    )


async def test_a_mutating_mounted_tool_pauses_the_run_for_approval(
    engine: AsyncEngine, bus: RedisBus, clean_registry: None
) -> None:
    """Half of the criterion, checked where it actually happens.

    `OrchestratorHooks.before_tool` resolves the name in `tools.registry.REGISTRY` and
    reads `requires_approval` off what it finds. A mounted tool that never reached the
    registry is a `None` there — and a `None` has no flag to read, so the call would run
    with nobody asked. That is why mounting means registering.
    """
    run_id = await make_run(engine)
    async with mounted(stub_config()) as (_, tools):
        registry.register(tools)
        hooks = hooks_for(run_id, engine, bus)

        asking = asyncio.create_task(
            hooks.before_tool("mcp_stub_write", {"path": "x.py", "content": "hi"})
        )
        # What a human sees: the tool's name and the arguments it was called with.
        event = await _until_parked(engine, bus, run_id)
        assert event.payload["tool_name"] == "mcp_stub_write"
        assert event.payload["input"] == {"path": "x.py", "content": "hi"}

        await bus.push_inbox(
            run_id, {"type": "approve", "tool_call_id": await bus.get_pending(run_id)}
        )
        assert await asyncio.wait_for(asking, timeout=20) is None  # None means "go ahead"


async def test_a_rejected_mounted_tool_call_never_reaches_the_server(
    engine: AsyncEngine, bus: RedisBus, clean_registry: None
) -> None:
    run_id = await make_run(engine)
    async with mounted(stub_config()) as (_, tools):
        registry.register(tools)
        hooks = hooks_for(run_id, engine, bus)

        asking = asyncio.create_task(
            hooks.before_tool("mcp_stub_write", {"path": "x", "content": ""})
        )
        await _until_parked(engine, bus, run_id)
        await bus.push_inbox(
            run_id,
            {
                "type": "reject",
                "tool_call_id": await bus.get_pending(run_id),
                "reason": "not on someone else's repository",
            },
        )
        refusal = await asyncio.wait_for(asking, timeout=20)
    assert refusal is not None and "not on someone else's repository" in refusal


async def test_a_read_only_mounted_tool_runs_without_asking_anybody(
    engine: AsyncEngine, bus: RedisBus, clean_registry: None
) -> None:
    """The other half. An Analyzer querying a read-only server should not cost a human's
    attention on every call — if it did, nobody would mount one."""
    run_id = await make_run(engine)
    async with mounted(stub_config(name="pg", roles=["analyzer"], allow=["echo"], mutating=[])) as (
        servers,
        tools,
    ):
        registry.register(tools)
        assert "mcp_pg_echo" in [t.name for t in registry.tools_for("analyzer")]

        hooks = hooks_for(run_id, engine, bus)
        assert await asyncio.wait_for(hooks.before_tool("mcp_pg_echo", {"text": "q"}), 10) is None
        assert await bus.get_pending(run_id) is None

        result = await servers.call("pg", "echo", {"text": "select 1"})
    assert result.content == "echo: select 1"


async def _until_parked(
    engine: AsyncEngine, bus: RedisBus, run_id: uuid.UUID, timeout_s: float = 20.0
) -> Any:
    """Wait for the `awaiting_input` event, and return it.

    The event rather than the pending key, which the gate sets first — waiting on that one
    and then reading the event is a race the test loses about half the time.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with session(engine) as s:
            event = await db.latest_event(s, run_id, "awaiting_input")
        if event is not None and await bus.get_pending(run_id):
            return event
        await asyncio.sleep(0.05)
    raise AssertionError("the run never parked for approval")
