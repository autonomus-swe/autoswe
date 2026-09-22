"""The MCP server: the control plane as tools another agent can call.

Every tool here is a shape conversion — parse the arguments, call `api.service`, render
the result — in the same way `api/routes/` is. The preconditions live in the service, so
an approval sent from an editor goes through the same replay guard as one sent by `curl`,
and there is no second copy to keep in step.

## Errors

The SDK keeps a crashing tool's message on the server and sends the client only "Error
executing tool <name>". That is right for a crash and wrong for `run not found`, which is
the answer, so every `ControlError` is turned into a `ToolError` whose text survives.
`http_error` in `api/errors.py` is the same translation for the other transport.

## Identifiers are strings

`run_id` is a `str`, not a `UUID`. The tool schema a client sees is generated from these
annotations, and a flat string is the shape every client handles — the same reason Phase 0
kept the contracts flat for guided decoding. Bad ids are rejected here with a message
saying so.

## Two transports, one server

`build_server` takes a callable rather than a `ControlPlane` because the stdio entrypoint
and the mounted HTTP app get their connections at different moments: stdio opens them at
startup and holds them, while the FastAPI mount can only read `app.state` once the app's
lifespan has run. A callable defers that lookup to the moment a tool is actually called,
which is after both are ready.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from api.service import ControlError, ControlPlane
from contracts import Budget
from gateway.providers import unavailable
from mcp_bridge import workspace
from observability.logging import get_logger

log = get_logger(__name__)

OK = {"ok": True}
INSTRUCTIONS = """\
Drives autoswe, an autonomous software engineering agent that turns a goal into a pull
request on a GitHub repository.

Start work with `create_run`, then `wait_for_run`. A run that comes back `awaiting_input`
is asking you something: the `pending` field holds either the planner's open questions
(answer with `answer_run`) or a tool call needing a decision (`approve_tool` /
`reject_tool`), and the run stays parked until it gets one. Wait again after answering.

`get_artifact` reads what the run produced — `diff`, `test_report`, `review`, `security`,
`pr_body`. `search_code` and `read_file` look inside the run's checkout while it exists.
"""


def build_server(
    get_plane: Callable[[], ControlPlane],
    *,
    provider: str,
    worktrees_dir: Path | None = None,
    name: str = "autoswe",
    version: str = "",
) -> MCPServer:
    """An MCP server over one control plane.

    `provider` is the deployment's default, used by any run that does not name one. Step
    6.3 taught the worker to build from the run's row, so `create_run` now takes a provider
    too — the record and the process that ran are the same thing again.

    `worktrees_dir` is the worker's checkout root. Without it `search_code` and `read_file`
    are not registered at all, rather than registered and always failing — a tool a client
    can see is a tool it will try.
    """
    server = MCPServer(name=name, version=version, instructions=INSTRUCTIONS)

    def run_uuid(run_id: str) -> uuid.UUID:
        try:
            return uuid.UUID(run_id.strip())
        except ValueError as e:
            raise ToolError(f"{run_id!r} is not a run id (expected a UUID)") from e

    # ---- starting and watching ------------------------------------------------------

    @server.tool()
    async def create_run(
        repo_url: str,
        goal: str,
        base_branch: str = "main",
        budget_usd: float = 10.0,
        unattended: bool = False,
        run_provider: str = "",
    ) -> dict[str, str]:
        """Start a run: clone `repo_url`, pursue `goal`, open a pull request.

        Returns the run id immediately; the work happens on a worker. Set `unattended`
        when nobody will be watching — approvals are then refused rather than parking the
        run, and the planner is told not to ask questions. `run_provider` overrides which
        LLM provider the run uses; leave it empty for the deployment's default.
        """
        if budget_usd <= 0:
            raise ToolError("budget_usd must be greater than zero")
        chosen = run_provider.strip() or provider
        if (why := unavailable(chosen)) is not None:
            raise ToolError(why)
        try:
            run_id = await _plane(get_plane).create_run(
                repo_url=repo_url,
                goal=goal,
                base_branch=base_branch,
                provider=chosen,
                budget=Budget(max_usd=budget_usd),
                unattended=unattended,
            )
        except ControlError as e:
            raise _tool_error(e) from e
        except ValueError as e:  # a Budget or repo_url the contracts refuse
            raise ToolError(str(e)) from e
        log.info("mcp_run_created", run_id=str(run_id))
        return {"run_id": str(run_id)}

    @server.tool()
    async def get_run(run_id: str) -> dict[str, Any]:
        """One run's status, phase, cost so far, and pull-request URL once it has one."""
        return await _summary(get_plane, run_uuid(run_id))

    @server.tool()
    async def list_runs(limit: int = 20) -> list[dict[str, Any]]:
        """Recent runs, newest first — for picking up where a previous session left off."""
        rows = await _plane(get_plane).list_runs(limit)
        return [_render_run(row) for row in rows]

    @server.tool()
    async def wait_for_run(run_id: str, timeout_s: float = 1800.0) -> dict[str, Any]:
        """Wait until the run finishes or needs an answer, then return it.

        Returns early with `status: "awaiting_input"` and a `pending` field when the run
        is asking something — answer it and wait again. If `status` is still `running` or
        `queued`, the timeout elapsed and the run is going; call this again.
        """
        uid = run_uuid(run_id)
        try:
            row = await _plane(get_plane).wait_for_run(uid, timeout_s=timeout_s)
        except ControlError as e:
            raise _tool_error(e) from e
        return await _with_pending(get_plane, row)

    # ---- answering -------------------------------------------------------------------

    @server.tool()
    async def answer_run(run_id: str, text: str, tool_call_id: str = "") -> dict[str, bool]:
        """Answer a run parked on a question.

        Pass `tool_call_id` only when the run is waiting on an `ask_user` tool call — the
        `pending` field says which. An answer to the planner's open questions has none.
        """
        await _control(get_plane, "answer", run_uuid(run_id), text, tool_call_id or None)
        return OK

    @server.tool()
    async def approve_tool(run_id: str, tool_call_id: str) -> dict[str, bool]:
        """Allow the tool call the run is parked on. Must be the call it is waiting for."""
        await _control(get_plane, "approve", run_uuid(run_id), tool_call_id)
        return OK

    @server.tool()
    async def reject_tool(run_id: str, tool_call_id: str, reason: str) -> dict[str, bool]:
        """Refuse the tool call. `reason` goes back to the model as the call's result."""
        await _control(get_plane, "reject", run_uuid(run_id), tool_call_id, reason)
        return OK

    @server.tool()
    async def cancel_run(run_id: str) -> dict[str, bool]:
        """Ask a run to stop. It ends at the next node or tool call, whichever is sooner."""
        await _control(get_plane, "cancel", run_uuid(run_id))
        return OK

    # ---- what the run produced -------------------------------------------------------

    @server.tool()
    async def list_events(run_id: str, after_id: int = 0, limit: int = 200) -> dict[str, Any]:
        """A page of the run's events, oldest first.

        Pass the returned `next_after_id` back to continue rather than re-reading from the
        start; a long run emits thousands.
        """
        uid = run_uuid(run_id)
        try:
            rows = await _plane(get_plane).list_events(uid, after_id=after_id, limit=limit)
        except ControlError as e:
            raise _tool_error(e) from e
        return {
            "events": [
                {"id": r.id, "type": r.type, "payload": r.payload, "ts": r.ts.isoformat()}
                for r in rows
            ],
            "next_after_id": rows[-1].id if rows else after_id,
        }

    @server.tool()
    async def list_artifacts(run_id: str) -> list[dict[str, Any]]:
        """What the run wrote, in order, with sizes but not content."""
        uid = run_uuid(run_id)
        try:
            rows = await _plane(get_plane).list_artifacts(uid)
        except ControlError as e:
            raise _tool_error(e) from e
        return [
            {
                "kind": r.kind,
                "path": r.path,
                "created_at": r.created_at.isoformat(),
                "size": len(_as_text(r.content)),
            }
            for r in rows
        ]

    @server.tool()
    async def get_artifact(run_id: str, kind: str) -> str:
        """The latest artifact of one kind: `diff`, `test_report`, `review`, `security`,
        `pr_body`, `plan`, `facts`. Returns text; structured kinds come back as JSON."""
        uid = run_uuid(run_id)
        try:
            row = await _plane(get_plane).get_artifact(uid, kind)
        except ControlError as e:
            raise _tool_error(e) from e
        return _as_text(row.content)

    @server.resource("run://{run_id}/artifacts/{kind}", mime_type="text/plain")
    async def artifact_resource(run_id: str, kind: str) -> str:
        """The same artifacts, for clients that prefer resources to tool calls."""
        uid = run_uuid(run_id)
        try:
            row = await _plane(get_plane).get_artifact(uid, kind)
        except ControlError as e:
            raise _tool_error(e) from e
        return _as_text(row.content)

    # ---- looking inside the checkout -------------------------------------------------

    if worktrees_dir is not None:

        @server.tool()
        async def search_code(
            run_id: str,
            pattern: str = "",
            query: str = "",
            glob: str = "",
            max_results: int = 50,
            fixed_string: bool = False,
        ) -> str:
            """Search the run's checkout. `pattern` is a regular expression (or a literal
            with `fixed_string`); `query` searches by meaning over the symbol index."""
            ctx = await _workspace_ctx(get_plane, run_uuid(run_id), worktrees_dir)
            result = await workspace.search_code(
                ctx,
                pattern=pattern,
                query=query,
                glob=glob or None,
                max_results=max_results,
                fixed_string=fixed_string,
            )
            return _tool_text(result)

        @server.tool()
        async def read_file(run_id: str, path: str, start_line: int = 1, end_line: int = -1) -> str:
            """Read a file from the run's checkout, with line numbers. `path` is relative
            to the repository root."""
            ctx = await _workspace_ctx(get_plane, run_uuid(run_id), worktrees_dir)
            result = await workspace.read_file(
                ctx, path=path, start_line=start_line, end_line=end_line
            )
            return _tool_text(result)

    return server


# ---- helpers ---------------------------------------------------------------------------


def _plane(get_plane: Callable[[], ControlPlane]) -> ControlPlane:
    try:
        return get_plane()
    except AttributeError as e:
        # `app.state.engine` before the lifespan ran, which is a deployment fault rather
        # than a caller's. Say which, because the alternative is a bare AttributeError.
        raise ToolError("the control plane is not ready yet; the service is still starting") from e


def _tool_error(error: ControlError) -> ToolError:
    """A control-plane complaint the caller can act on, with its text intact."""
    return ToolError(str(error))


async def _control(
    get_plane: Callable[[], ControlPlane], name: str, run_id: uuid.UUID, *args: Any
) -> None:
    """Call one of the human-in-the-loop operations, mapping its refusal to a ToolError.

    By name rather than four near-identical try blocks: they differ only in which method
    and which arguments, and writing the translation once is the point of the module.
    """
    method = getattr(_plane(get_plane), name)
    try:
        await method(run_id, *args)
    except ControlError as e:
        raise _tool_error(e) from e
    log.info("mcp_control", op=name, run_id=str(run_id))


async def _summary(get_plane: Callable[[], ControlPlane], run_id: uuid.UUID) -> dict[str, Any]:
    try:
        row = await _plane(get_plane).get_run(run_id)
    except ControlError as e:
        raise _tool_error(e) from e
    return await _with_pending(get_plane, row)


async def _with_pending(get_plane: Callable[[], ControlPlane], row: Any) -> dict[str, Any]:
    """A run summary, plus what it is waiting for when it is waiting for something.

    The question is fetched only for a parked run. Doing it unconditionally would add a
    query to every poll of every run to carry a field that is almost always null.
    """
    summary = _render_run(row)
    if row.status == "awaiting_input":
        summary["pending"] = await _plane(get_plane).pending_question(row.id)
    return summary


def _render_run(row: Any) -> dict[str, Any]:
    return {
        "run_id": str(row.id),
        "status": row.status,
        "phase": row.phase,
        "goal": row.goal,
        "repo_url": row.repo_url,
        "work_branch": row.work_branch,
        "cost_usd": float(row.cost_usd or 0),
        "pr_url": row.pr_url,
        "error": row.error,
        "updated_at": row.updated_at.isoformat(),
    }


def _as_text(content: Any) -> str:
    """An artifact as text. `diff` is stored as `{"text": …}` and is a diff, not JSON."""
    if isinstance(content, dict) and set(content) == {"text"}:
        return str(content["text"])
    return json.dumps(content, indent=2, default=str)


def _tool_text(result: Any) -> str:
    """A `ToolResult` as the string an MCP tool returns, errors raised rather than
    returned — the SDK's `isError` is set by raising, and a caller that got an error
    string back as a normal result would have to parse prose to notice."""
    if result.is_error:
        raise ToolError(str(result.content))
    return str(result.content)


async def _workspace_ctx(
    get_plane: Callable[[], ControlPlane], run_id: uuid.UUID, worktrees_dir: Path
) -> Any:
    plane = _plane(get_plane)
    try:
        row = await plane.get_run(run_id)
    except ControlError as e:
        raise _tool_error(e) from e
    return workspace.context_for(row, worktrees_dir=worktrees_dir, engine=plane.engine)
