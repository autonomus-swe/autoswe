"""The mounted-tool call site, and how a remote result becomes text a model reads.

Criterion 2 is about *approval*, and it is genuinely tested:
`tests/integration/test_mcp_client.py` drives a stub MCP server as a real subprocess and
asserts the pause through `OrchestratorHooks.before_tool`. That is not what this file is
about, and the criterion is not overstated.

What nothing tested is the ordinary path — `MountedTool.run` forwarding the model's
arguments to the remote tool, and `_render` turning the reply into a string. `MountedTool`
appears in no test file at all, and four mutations survived the whole suite:

| mutation | consequence |
|---|---|
| `call(..., kwargs)` → `call(..., {})` | **every argument the model chose is dropped** |
| remove `await self._drop(server)` | a dead session is reused forever after one blip |
| the truncation made unreachable | a 2 MB result enters the context window whole |
| the `structured_content` fallback → `""` | a structural-only answer reads as empty |

The first is the one that matters and it is invisible from either end: the tool is
advertised with the right schema, the call succeeds, the server returns a perfectly good
result — for a query nobody asked for. It is the same shape as the `runs` columns that were
written and read by nothing, and as the MCP server arguments closed in #99, one layer
further out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from mcp_bridge import client as mcp_client
from mcp_bridge.client import MAX_RESULT_CHARS, MountedTool, _render

pytestmark = pytest.mark.unit


@dataclass
class RecordingServers:
    """Stands in for `MCPServers`, recording exactly what the call site handed over."""

    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    result: Any = None

    async def call(self, server: str, remote: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((server, remote, arguments))
        from contracts import ToolResult

        return self.result or ToolResult(content="ok", is_error=False)


def mounted(servers: RecordingServers) -> MountedTool:
    """A `MountedTool` subclass, built the way `_tool_class` builds them.

    Subclassed rather than parameterised because the `Tool` protocol declares `server` and
    `remote` as class variables — so a test that set them on the instance would be testing
    a shape the production code does not use.
    """

    class PostgresQueryTool(MountedTool):
        server = "postgres"
        remote = "query"
        name = "mcp_postgres_query"
        description = "Run a read-only query."
        mutating = False

    return PostgresQueryTool(servers)  # type: ignore[arg-type]


# ---- the arguments the model chose ---------------------------------------------------------


async def test_the_models_arguments_reach_the_remote_tool() -> None:
    """The call site, which no test invoked.

    Handing `{}` over instead leaves everything else intact: the tool is advertised with the
    server's own schema, the call succeeds, and the remote tool runs on its defaults. A
    `SELECT` nobody asked for, reported as the answer to one somebody did.
    """
    servers = RecordingServers()
    tool = mounted(servers)

    await tool.run(
        None,  # type: ignore[arg-type]
        sql="select count(*) from runs where status = 'failed'",
        limit=25,
    )

    assert servers.calls, "the tool never reached the server registry"
    server, remote, arguments = servers.calls[0]
    assert (server, remote) == ("postgres", "query")
    assert arguments == {
        "sql": "select count(*) from runs where status = 'failed'",
        "limit": 25,
    }, "the arguments the model chose must be the arguments the remote tool receives"


async def test_a_call_with_no_arguments_forwards_an_empty_mapping() -> None:
    """So the assertion above cannot be satisfied by a call site that invents arguments,
    and so a genuinely nullary remote tool still works."""
    servers = RecordingServers()
    await mounted(servers).run(None)  # type: ignore[arg-type]
    assert servers.calls[0][2] == {}


async def test_the_tool_carries_its_servers_name_and_not_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`server` and `remote` are the routing. Swapped or defaulted, the call lands on the
    wrong server's tool of the same name — which on a mounted GitHub tool beside a mounted
    Postgres one is not a theoretical concern."""
    servers = RecordingServers()
    await mounted(servers).run(None, x=1)  # type: ignore[arg-type]
    assert servers.calls[0][0] == "postgres", "the configured server, not the tool's own name"
    assert servers.calls[0][1] == "query", "the remote name, not the local mcp_… alias"


# ---- rendering a remote reply --------------------------------------------------------------


@dataclass
class Block:
    text: str | None = None
    type: str = "text"


@dataclass
class Reply:
    content: list[Any] = field(default_factory=list)
    structured_content: Any = None
    is_error: bool = False


def test_text_blocks_are_joined() -> None:
    rendered = _render(Reply(content=[Block(text="first"), Block(text="second")]))
    assert rendered.content == "first\nsecond"
    assert rendered.is_error is False


def test_a_non_text_block_is_named_rather_than_dropped() -> None:
    """ "The tool returned an image" is information; a silently empty result is not."""
    rendered = _render(Reply(content=[Block(text=None, type="image")]))
    assert "[image]" in rendered.content


def test_a_structural_answer_is_used_when_there_is_no_text() -> None:
    """Some servers answer only in `structured_content`. Replacing the fallback with `""`
    turns those into "(no content)" — a tool that worked, reported as one that said
    nothing, which the model then retries."""
    rendered = _render(Reply(content=[], structured_content={"rows": [[7]]}))
    assert "rows" in rendered.content
    assert "7" in rendered.content


def test_a_reply_with_nothing_in_it_says_so_explicitly() -> None:
    """And the other side: genuinely empty must not become the string "None"."""
    rendered = _render(Reply(content=[], structured_content=None))
    assert rendered.content == "(no content)"


def test_an_oversized_result_is_truncated_and_says_that_it_was() -> None:
    """A remote tool can return megabytes. Unbounded, that goes into the context window
    whole — and `gateway/context.py` then starts clearing *other* tool results to make room
    for it, so one greedy reply costs the run the history it needed.

    The marker matters as much as the cut: a model given a silently truncated table will
    reason about the rows it cannot see.
    """
    rendered = _render(Reply(content=[Block(text="x" * (MAX_RESULT_CHARS * 2))]))

    assert len(rendered.content) < MAX_RESULT_CHARS * 2
    assert "truncated" in rendered.content
    assert str(MAX_RESULT_CHARS) in rendered.content


def test_a_result_just_under_the_limit_is_left_alone() -> None:
    """So the truncation cannot become "always truncate", which would pass the test above
    while mangling every ordinary reply."""
    body = "y" * (MAX_RESULT_CHARS - 10)
    rendered = _render(Reply(content=[Block(text=body)]))
    assert rendered.content == body
    assert "truncated" not in rendered.content


def test_an_error_reply_stays_an_error() -> None:
    """`is_error` is what makes the orchestrator treat the result as a failure rather than
    as an answer. Rendered away, a failed tool call reads as a successful one."""
    rendered = _render(Reply(content=[Block(text="permission denied")], is_error=True))
    assert rendered.is_error is True
    assert "permission denied" in rendered.content


# ---- a failed session is dropped -----------------------------------------------------------


async def test_a_transport_failure_drops_the_session_so_the_next_call_reconnects() -> None:
    """ "The session is what failed, so the session is what is dropped."

    Without the drop the dead client is handed to every later call and each one fails the
    same way — one blip ends the run's access to that server for the rest of the run, and
    the model sees a tool that is permanently broken rather than one that needs retrying.
    """
    from mcp_bridge.config import ServerConfig

    cfg = ServerConfig(
        name="postgres",
        command=["true"],
        env={},
        roles=["analyzer"],
        allow=["query"],
        mutating=[],
    )
    servers = mcp_client.MCPServers([cfg])

    class DeadClient:
        async def call_tool(self, remote: str, arguments: dict[str, Any]) -> Any:
            raise ConnectionResetError("the pipe went away")

    dropped: list[str] = []

    class FakeConn:
        client = DeadClient()

    async def fake_connect(server: str) -> Any:
        return FakeConn()

    async def fake_drop(server: str) -> None:
        dropped.append(server)

    servers._connect = fake_connect  # type: ignore[method-assign]
    servers._drop = fake_drop  # type: ignore[method-assign]

    result = await servers.call("postgres", "query", {"sql": "select 1"})

    assert result.is_error is True, "the model is told the call failed"
    assert "pipe went away" in result.content, "and what failed, so a retry is informed"
    assert dropped == ["postgres"], "the dead session must not be handed to the next call"


async def test_a_tool_outside_the_allow_list_never_reaches_a_connection() -> None:
    """The allow-list is the security boundary, and it is enforced here as well as in
    `start()` — "a boundary enforced in one place is a boundary that moves the next time
    that place is refactored". So it must refuse before connecting, not after."""
    from mcp_bridge.config import ServerConfig

    cfg = ServerConfig(
        name="postgres",
        command=["true"],
        env={},
        roles=["analyzer"],
        allow=["query"],
        mutating=[],
    )
    servers = mcp_client.MCPServers([cfg])

    connected: list[str] = []

    async def fake_connect(server: str) -> Any:
        connected.append(server)
        raise AssertionError("must not connect for a disallowed tool")

    servers._connect = fake_connect  # type: ignore[method-assign]

    result = await servers.call("postgres", "drop_everything", {})
    assert result.is_error is True
    assert "not allowed" in result.content
    assert connected == [], "refused before any session was opened"
