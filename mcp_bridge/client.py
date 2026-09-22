"""Mounting external MCP servers so the agents can call their tools.

A GitHub server that can read issues, a Postgres server that can run a read-only query:
tools the agents did not have to be written to know about. Each arrives as an ordinary
`Tool`, so it meets the same gates everything else does — policy, budget, approval, the
ledger — rather than a parallel path with its own rules.

## The gates are why the wrapper exists

`orchestrator/hooks.py` looks a tool up in `tools.registry.REGISTRY` by name and reads
`requires_approval` off it. A mounted tool reaching the provider by some other route would
be a `None` in that lookup, and a `None` has no `requires_approval`: the mutating GitHub
tool would run and nobody would be asked. So a mounted tool is a registered `Tool` or it
is not mounted at all.

## Mounted at startup, because the schema comes from the server

`start()` connects to each server and calls `list_tools()`. The schemas it returns are
what the model reads to know that `get_issue` wants an owner, a repo and a number; a
locally-invented placeholder would leave it guessing, and would be wrong the first time
the server changed. That is worth a connection at startup.

A server that will not start contributes no tools and logs loudly. It does not stop the
worker: an unreachable GitHub server is a reason to run without GitHub tools, not a reason
to refuse every run.

## One session per server, per process

Each session lives in its own supervisor task, which is not a detail: a `ClientSession`
holds an anyio cancel scope that must be left by the task that entered it, and a session
opened inside a job's task would die with the job. The supervisor enters it, publishes it,
and waits. A transport failure drops the connection rather than retrying inside the call,
and the next call reconnects — reconnection where it belongs, rather than a retry loop
inside a tool the model is waiting on.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from mcp import StdioServerParameters
from mcp.client import Client

from contracts import ToolResult
from mcp_bridge.config import ServerConfig
from observability.logging import get_logger
from tools.base import BaseTool, RunContext

log = get_logger(__name__)

MAX_RESULT_CHARS = 20_000
CLOSE_TIMEOUT_S = 10


class MountedTool(BaseTool):
    """One remote tool, called over a session this process holds open.

    Subclassed per tool by `_tool_class` rather than parameterised per instance: the
    `Tool` protocol declares `name`, `mutating` and the rest as class variables, and that
    is worth keeping true. A remote tool really is a distinct kind of tool, not a
    configuration of one.
    """

    server: str
    remote: str

    def __init__(self, servers: MCPServers) -> None:
        self.servers = servers

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        return await self.servers.call(self.server, self.remote, kwargs)


class _Connection:
    """A server's session and the task that owns its lifetime."""

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.client: Client | None = None
        self.tools: list[Any] = []
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self._serve(), name=f"mcp-{cfg.name}")

    async def _serve(self) -> None:
        params = StdioServerParameters(
            command=self.cfg.command[0],
            args=list(self.cfg.command[1:]),
            # Only what the configuration names. The SDK merges its own safe-list
            # (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) so `npx` can be found;
            # nothing else of the worker's is passed, so a mounted server does not get
            # the model key, the database DSN or the GitHub token by default.
            env=dict(self.cfg.env),
        )
        try:
            async with Client(params, read_timeout_seconds=self.cfg.timeout_s) as client:
                self.client = client
                self.tools = list((await client.list_tools()).tools)
                log.info(
                    "mcp_server_mounted",
                    server=self.cfg.name,
                    offered=len(self.tools),
                    allowed=len(self.cfg.allow),
                )
                self._settle(None)
                await self.stop.wait()
        except Exception as e:
            log.error("mcp_server_failed", server=self.cfg.name, error=f"{type(e).__name__}: {e}")
            self._settle(e)
        finally:
            self.client = None

    @property
    def dead(self) -> bool:
        """Whether this connection can still carry a call.

        Both halves are needed. `task.done()` catches a supervisor that has returned —
        the child exited, the pipe closed — and the `client is None` half catches one that
        never got a session in the first place. Without this, a connection that died was
        left registered with its `ready` future already resolved, so every later call
        found it, found no client, and failed the same way forever. The worker would have
        needed a restart to use a server whose child process had merely crashed.
        """
        return self.task.done() or self.client is None

    def _settle(self, error: Exception | None) -> None:
        if self.ready.done():
            return
        if error is None:
            self.ready.set_result(None)
        else:
            self.ready.set_exception(error)

    async def close(self) -> None:
        self.stop.set()
        # The supervisor is waiting on `stop` and should return on its own. Bounded,
        # because a child that ignores its pipe closing must not hold up the worker; and
        # shielded, so a timeout here leaves the task to finish rather than cancelling it
        # mid-teardown.
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=CLOSE_TIMEOUT_S)
        except Exception as e:
            log.warning("mcp_close_untidy", server=self.cfg.name, error=f"{type(e).__name__}: {e}")


class MCPServers:
    """Every mounted server in this process, and the tools they contribute."""

    def __init__(self, configs: list[ServerConfig]) -> None:
        self.configs = {c.name: c for c in configs}
        self._connections: dict[str, _Connection] = {}
        self._tools: list[tuple[BaseTool, list[str]]] = []
        self._lock = asyncio.Lock()

    async def start(self) -> list[tuple[BaseTool, list[str]]]:
        """Connect to every configured server and build `(tool, roles)` for what it offers.

        Only allow-listed names are wrapped. A name in `allow` the server does not offer
        is logged rather than silently dropped: it means the configuration and the server
        disagree, which is a thing somebody needs to know before a model is told the tool
        exists.
        """
        self._tools = []
        for name, cfg in self.configs.items():
            try:
                conn = await self._connect(name)
            except Exception as e:
                # Logged again rather than relying on the supervisor's line: this one says
                # which server the worker is going to run *without*, which is the fact an
                # operator reading a startup log is looking for.
                log.error("mcp_server_unmounted", server=name, error=f"{type(e).__name__}: {e}")
                continue
            offered = {t.name: t for t in conn.tools}
            if missing := [a for a in cfg.allow if a not in offered]:
                log.warning("mcp_allowed_but_not_offered", server=name, tools=missing)
            for remote in cfg.allow:
                if remote not in offered:
                    continue
                cls = _tool_class(cfg, remote, offered[remote])
                self._tools.append((cls(self), list(cfg.roles)))
        log.info("mcp_tools_mounted", count=len(self._tools))
        return list(self._tools)

    async def call(self, server: str, remote: str, arguments: dict[str, Any]) -> ToolResult:
        """Call one remote tool, turning every failure into a result the model can read."""
        cfg = self.configs.get(server)
        if cfg is None or remote not in cfg.allow:
            # Unreachable through `start()`, which only wraps allowed names. Here because
            # the allow-list is the security boundary, and a boundary enforced in one
            # place is a boundary that moves the next time that place is refactored.
            return ToolResult(content=f"error: {remote} is not allowed on {server}", is_error=True)
        try:
            client = (await self._connect(server)).client
            assert client is not None  # `_connect` raises rather than returning a dead one
        except Exception as e:
            return ToolResult(
                content=f"error: MCP server {server!r} is unavailable: {type(e).__name__}: {e}",
                is_error=True,
            )
        try:
            result = await client.call_tool(remote, arguments)
        except Exception as e:
            # The session is what failed, so the session is what is dropped. The next call
            # starts a fresh one.
            await self._drop(server)
            log.warning("mcp_call_failed", server=server, tool=remote, error=type(e).__name__)
            return ToolResult(
                content=f"error: calling {remote} on {server} failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return _render(result)

    async def aclose(self) -> None:
        for name in list(self._connections):
            await self._drop(name)

    async def _connect(self, server: str) -> _Connection:
        """The live session for this server, starting or replacing one as needed.

        Liveness is decided here rather than at the call site, because "is this connection
        usable" is this method's question and a caller that had to ask it would eventually
        forget.
        """
        async with self._lock:
            conn = self._connections.get(server)
            if conn is not None and conn.dead:
                self._connections.pop(server, None)
                conn = None
            if conn is None:
                conn = _Connection(self.configs[server])
                self._connections[server] = conn
        # Outside the lock: a server that takes ten seconds to start must not hold up the
        # others, and `ready` resolves once for everyone waiting on it.
        await conn.ready
        if conn.client is None:
            raise RuntimeError("the session closed while starting")
        return conn

    async def _drop(self, server: str) -> None:
        conn = self._connections.pop(server, None)
        if conn is not None:
            await conn.close()


def _tool_class(cfg: ServerConfig, remote: str, offered: Any) -> type[MountedTool]:
    """A `MountedTool` subclass carrying one remote tool's policy and schema.

    `mutating` comes from the configuration, never from the server's own
    `ToolAnnotations`. A server that declared its write tool read-only — by mistake or
    otherwise — would be disarming its own approval gate, and the point of mounting
    through the harness is that the harness decides.

    The schema does come from the server: it describes what the tool accepts, which is not
    a claim about what it may do, and a local copy would be wrong the first time the
    server changed.
    """
    mutating = remote in cfg.mutating
    description = (offered.description or remote).strip()
    return cast(
        type[MountedTool],
        type(
            f"Mcp{_camel(cfg.name)}{_camel(remote)}Tool",
            (MountedTool,),
            {
                "server": cfg.name,
                "remote": remote,
                "name": cfg.tool_name(remote),
                "description": f"[{cfg.name} MCP server] {description}",
                "input_schema": dict(offered.input_schema or {}),
                "mutating": mutating,
                "parallel_safe": not mutating,
                # Mutating means a human decides. `orchestrator/hooks.py` reads this off
                # the registry entry, which is why mounted tools go into the registry.
                "requires_approval": mutating,
            },
        ),
    )


def _camel(name: str) -> str:
    return "".join(part.title() for part in name.replace("-", "_").split("_"))


def _render(result: Any) -> ToolResult:
    """An MCP `CallToolResult` as the string the model reads.

    Text blocks joined; anything else named rather than dropped, because "the tool
    returned an image" is information and a silently empty result is not.
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"[{getattr(block, 'type', 'content')}]")
    body = "\n".join(parts).strip()
    if not body:
        structured = getattr(result, "structured_content", None)
        body = "" if structured is None else str(structured)
    if len(body) > MAX_RESULT_CHARS:
        body = body[:MAX_RESULT_CHARS] + f"\n…[truncated at {MAX_RESULT_CHARS} chars]"
    is_error = bool(getattr(result, "is_error", False))
    return ToolResult(content=body or "(no content)", is_error=is_error)
