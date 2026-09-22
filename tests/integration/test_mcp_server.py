"""The MCP server against a real Postgres and Redis, over both transports.

The unit tests check the shape conversion against a fake plane. These check the thing that
fake cannot: that the tools are wired to the same control plane the HTTP routes are, so an
approval sent from an editor meets the same guard as one sent with `curl`, and a run
started over MCP is a row the API can read back.

The HTTP transport gets its own tests because it is a separate ASGI application — a key
check written as a FastAPI `Depends` beside it would never run, and every tool including
`cancel_run` would be open. That failure is invisible from the code, so it is asserted.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from sqlalchemy.ext.asyncio import AsyncEngine

from api.service import ControlPlane
from contracts import Budget
from core.settings import get_settings
from mcp_bridge.server import build_server
from storage import repo as db
from storage.db import session
from storage.redis import RedisBus
from tests.integration.conftest import FakeArq, api_app

pytestmark = pytest.mark.integration
KEY = "test-key-123456"
REPO_ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def mcp_client(
    engine: AsyncEngine, bus: RedisBus, arq: FakeArq | None = None
) -> AsyncIterator[Client]:
    """An in-process MCP client over the real control plane.

    A context manager used inside each test rather than a fixture. `Client` holds an anyio
    task group, and a yielding async fixture is entered and left in different tasks, which
    anyio refuses at teardown — the test passes and the teardown explodes, which is the
    most confusing way for this to be wrong.
    """
    plane = ControlPlane(engine=engine, bus=bus, arq=arq or FakeArq())
    server = build_server(lambda: plane, provider="openai_compat")
    async with Client(server, raise_exceptions=False) as client:
        yield client


async def make_run(engine: AsyncEngine, status: str = "running") -> uuid.UUID:
    async with session(engine) as s:
        run_id = await db.create_run(
            s,
            repo_url="https://github.com/acme/demo",
            base_branch="main",
            goal="do the thing",
            budget=Budget(),
        )
        await db.set_run_phase(s, run_id, "code", status)
    return run_id


def text_of(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def body_of(result: Any) -> dict[str, Any]:
    assert not result.is_error, text_of(result)
    return dict(result.structured_content or {})


# ---- the run lifecycle over MCP ---------------------------------------------------------


async def test_create_run_writes_a_row_and_enqueues_it(engine: AsyncEngine, bus: RedisBus) -> None:
    arq = FakeArq()
    async with mcp_client(engine, bus, arq) as client:
        result = await client.call_tool(
            "create_run",
            {
                "repo_url": "https://github.com/acme/demo",
                "goal": "Implement subtract(a, b) with tests",
                "budget_usd": 3.0,
                "unattended": True,
            },
        )
    run_id = uuid.UUID(body_of(result)["run_id"])

    async with session(engine) as s:
        row = await db.get_run(s, run_id)
    assert row is not None
    assert row.status == "queued" and row.work_branch == f"agent/{run_id}"
    # `unattended` has been a column and an orchestrator behaviour since Phase 1 with no
    # way to set it from outside; this is the wire that was missing.
    assert row.unattended is True
    assert row.budget["max_usd"] == 3.0
    assert arq.jobs == [("run_job", str(run_id))]


async def test_get_run_reads_back_what_the_database_holds(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    run_id = await make_run(engine, status="running")
    async with mcp_client(engine, bus) as client:
        result = await client.call_tool("get_run", {"run_id": str(run_id)})
    body = body_of(result)
    assert body["status"] == "running" and body["phase"] == "code"
    assert body["goal"] == "do the thing"


async def test_a_run_that_does_not_exist_says_so(engine: AsyncEngine, bus: RedisBus) -> None:
    async with mcp_client(engine, bus) as client:
        result = await client.call_tool("get_run", {"run_id": str(uuid.uuid4())})
    assert result.is_error and "run not found" in text_of(result)


async def test_answer_reaches_the_runs_inbox(engine: AsyncEngine, bus: RedisBus) -> None:
    run_id = await make_run(engine, status="awaiting_input")
    async with mcp_client(engine, bus) as client:
        result = await client.call_tool(
            "answer_run", {"run_id": str(run_id), "text": "Postgres, not MySQL"}
        )
    assert not result.is_error, text_of(result)
    assert await bus.pop_inbox(run_id, timeout_s=2) == {
        "type": "answer",
        "text": "Postgres, not MySQL",
    }


async def test_answering_a_run_that_is_not_waiting_is_refused(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    run_id = await make_run(engine, status="running")
    async with mcp_client(engine, bus) as client:
        result = await client.call_tool("answer_run", {"run_id": str(run_id), "text": "Postgres"})
    assert result.is_error and "not awaiting input" in text_of(result)
    assert await bus.pop_inbox(run_id, timeout_s=1) is None


async def test_an_approval_for_a_different_call_is_refused(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    """The guard that makes the shared control plane worth extracting.

    An MCP tool that pushed straight to the inbox would let an approval be replayed later
    against whatever the run happened to be waiting on by then — a human authorising
    something they never saw, from an editor. The check lives in `api.service`, so it
    applies here without this module containing a line about it.
    """
    run_id = await make_run(engine, status="awaiting_input")
    await bus.set_pending(run_id, "toolu_the_one_shown")

    async with mcp_client(engine, bus) as client:
        stale = await client.call_tool(
            "approve_tool", {"run_id": str(run_id), "tool_call_id": "toolu_something_else"}
        )
        assert stale.is_error and "toolu_the_one_shown" in text_of(stale)
        assert await bus.pop_inbox(run_id, timeout_s=1) is None

        right = await client.call_tool(
            "approve_tool", {"run_id": str(run_id), "tool_call_id": "toolu_the_one_shown"}
        )
    assert not right.is_error, text_of(right)
    assert await bus.pop_inbox(run_id, timeout_s=2) == {
        "type": "approve",
        "tool_call_id": "toolu_the_one_shown",
    }


async def test_reject_carries_its_reason_to_the_run(engine: AsyncEngine, bus: RedisBus) -> None:
    run_id = await make_run(engine, status="awaiting_input")
    await bus.set_pending(run_id, "toolu_1")
    async with mcp_client(engine, bus) as client:
        result = await client.call_tool(
            "reject_tool",
            {"run_id": str(run_id), "tool_call_id": "toolu_1", "reason": "not on production"},
        )
    assert not result.is_error, text_of(result)
    assert await bus.pop_inbox(run_id, timeout_s=2) == {
        "type": "reject",
        "tool_call_id": "toolu_1",
        "reason": "not on production",
    }


async def test_cancel_sets_the_flag_the_runner_polls(engine: AsyncEngine, bus: RedisBus) -> None:
    run_id = await make_run(engine)
    async with mcp_client(engine, bus) as client:
        result = await client.call_tool("cancel_run", {"run_id": str(run_id)})
    assert not result.is_error, text_of(result)
    assert await bus.is_cancelled(run_id)


# ---- waiting ----------------------------------------------------------------------------


async def test_wait_returns_on_awaiting_input_with_the_question(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    """A wait that only stopped on a terminal status would deadlock against a run parked
    on a question the caller is the one expected to answer."""
    run_id = await make_run(engine, status="awaiting_input")
    async with session(engine) as s:
        await db.insert_event(
            s,
            run_id,
            "awaiting_input",
            {"kind": "open_questions", "questions": ["Which database?"]},
        )

    async with mcp_client(engine, bus) as client:
        result = await client.call_tool("wait_for_run", {"run_id": str(run_id), "timeout_s": 5.0})
    body = body_of(result)
    assert body["status"] == "awaiting_input"
    assert body["pending"] == {"kind": "open_questions", "questions": ["Which database?"]}


async def test_wait_returns_the_run_as_it_stands_when_the_clock_runs_out(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    """Non-terminal status is how the caller sees the timeout. An exception would discard
    the phase and cost the run had reached, which is what the caller wanted."""
    run_id = await make_run(engine, status="running")
    async with mcp_client(engine, bus) as client:
        result = await client.call_tool("wait_for_run", {"run_id": str(run_id), "timeout_s": 0.2})
    assert body_of(result)["status"] == "running"


async def test_wait_returns_immediately_once_the_run_is_done(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    run_id = await make_run(engine, status="done")
    async with mcp_client(engine, bus) as client:
        result = await client.call_tool("wait_for_run", {"run_id": str(run_id), "timeout_s": 600.0})
    assert body_of(result)["status"] == "done"


# ---- reading what the run produced -------------------------------------------------------


async def test_artifacts_are_readable_by_tool_and_by_resource(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    run_id = await make_run(engine, status="done")
    async with session(engine) as s:
        await db.save_artifact(s, run_id, "diff", None, {"text": "--- a/x.py\n+++ b/x.py\n"})

    async with mcp_client(engine, bus) as client:
        by_tool = await client.call_tool("get_artifact", {"run_id": str(run_id), "kind": "diff"})
        by_resource = await client.read_resource(f"run://{run_id}/artifacts/diff")
        listing = await client.call_tool("list_artifacts", {"run_id": str(run_id)})

    assert "--- a/x.py" in text_of(by_tool)
    assert "--- a/x.py" in "".join(getattr(c, "text", "") for c in by_resource.contents)
    assert [a["kind"] for a in body_of(listing)["result"]] == ["diff"]


async def test_events_page_forward_with_the_returned_cursor(
    engine: AsyncEngine, bus: RedisBus
) -> None:
    run_id = await make_run(engine)
    async with session(engine) as s:
        for phase in ("analyze", "plan", "code"):
            await db.insert_event(s, run_id, "phase_changed", {"phase": phase})

    async with mcp_client(engine, bus) as client:
        first = await client.call_tool("list_events", {"run_id": str(run_id), "limit": 2})
        body = body_of(first)
        assert [e["payload"]["phase"] for e in body["events"]] == ["analyze", "plan"]

        rest = await client.call_tool(
            "list_events", {"run_id": str(run_id), "after_id": body["next_after_id"]}
        )
    assert [e["payload"]["phase"] for e in body_of(rest)["events"]] == ["code"]


# ---- both transports, one answer ----------------------------------------------------------


async def test_mcp_and_http_report_the_same_run(engine: AsyncEngine, bus: RedisBus) -> None:
    """The point of the whole arrangement, asserted once rather than assumed."""
    run_id = await make_run(engine, status="running")
    async with mcp_client(engine, bus) as client:
        over_mcp = body_of(await client.call_tool("get_run", {"run_id": str(run_id)}))
    async with api_app(engine) as (http, _arq):
        over_http = (await http.get(f"/runs/{run_id}", headers={"X-API-Key": KEY})).json()

    for field in ("status", "phase", "goal", "repo_url", "work_branch", "cost_usd", "pr_url"):
        assert over_mcp[field] == over_http[field], field
    assert over_mcp["run_id"] == over_http["run_id"]


# ---- the HTTP transport --------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key-000000"}])
async def test_the_http_transport_refuses_an_unknown_key(
    engine: AsyncEngine, headers: dict[str, str]
) -> None:
    """A separate ASGI app, so this is not the same check the routes beside it get — and
    a transport published without one would look identical from the outside."""
    async with api_app(engine) as (api, _arq):
        response = await api.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream", **headers},
        )
    assert response.status_code == 401 and response.json() == {"detail": "unauthorized"}


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_the_http_transport_speaks_mcp_at_either_spelling(
    engine: AsyncEngine, path: str
) -> None:
    """`http://host/mcp` is what goes in an editor's configuration. A 307 to `/mcp/` would
    be followed by some clients and reported as a failure by others, so both reach the
    server directly."""
    async with api_app(engine) as (api, _arq):
        response = await api.post(
            path,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers={
                "X-API-Key": KEY,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 200, f"{response.status_code} {response.text}"
    assert "autoswe" in response.text


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        ("", 200),  # the default: no host check, the API key is the gate
        ("test", 200),
        ("api.example.com", 421),
        # `host:*` matches only when the request carries a port, and this one does not
        # (httpx omits the default). Worth pinning: an operator who writes `example.com:*`
        # expecting it to cover `example.com` would lock themselves out.
        ("test:*", 421),
    ],
)
async def test_host_validation_follows_the_setting(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, allowed: str, expected: int
) -> None:
    """The SDK turns DNS-rebinding protection on by default with a localhost-only
    allow-list, so a deployment behind a real hostname answers every MCP request with a
    421 whose body is three words. `MCP_ALLOWED_HOSTS` is how an operator opts in, and
    empty — the default here — means the API key is the gate.
    """
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", allowed)
    get_settings.cache_clear()
    async with api_app(engine) as (api, _arq):
        response = await api.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers={
                "X-API-Key": KEY,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == expected, response.text


# ---- the stdio transport -------------------------------------------------------------------


async def test_the_stdio_entrypoint_serves_the_same_tools(
    migrated_pg_url: str, redis_url: str
) -> None:
    """`autoswe-mcp` as an editor launches it: a child process speaking MCP on its pipes.

    In-process tests cannot catch what breaks this one — a console script that does not
    resolve, a log line on stdout, a settings field the process cannot load. The editor
    would report "server failed to start" and nothing else.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cli.mcp"],
        env={
            "DATABASE_URL": migrated_pg_url,
            "REDIS_URL": redis_url,
            "API_KEYS": KEY,
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
        },
    )
    async with Client(params, raise_exceptions=False) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        created = await client.call_tool(
            "create_run",
            {
                "repo_url": "https://github.com/acme/demo",
                "goal": "Implement subtract(a, b) with tests",
            },
        )
        run_id = body_of(created)["run_id"]
        fetched = await client.call_tool("get_run", {"run_id": run_id})

    assert {"create_run", "get_run", "wait_for_run", "approve_tool"} <= names
    assert body_of(fetched)["status"] == "queued"
