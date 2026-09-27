"""Every tool the MCP server advertises has its body run, with arguments that travel.

`tests/unit/test_mcp_server.py` asserts the tool *table* — that each advertised name
exists and does not crash. That is a weaker claim than it reads as, and a coverage run
said so: the bodies of `create_run`, `list_runs`, `search_code` and `read_file` were never
executed with a value anyone checked. Six mutations in `mcp_bridge/server.py` survived the
whole suite, each of them an argument the caller sent and the server dropped:

| dropped | consequence |
|---|---|
| `create_run`'s `goal` | the run pursues nothing the caller asked for |
| `list_runs`' `limit` | the advertised limit is decoration |
| `read_file`'s `start_line` / `end_line` | the whole file comes back as a "range" |
| `search_code`'s `pattern` / `query` / `glob` / `max_results` | not the search requested |
| `_workspace_ctx`'s `engine` | **semantic search silently degrades to text search** |

That last one is the reason this file exists rather than four more one-off tests. Nothing
fails when the engine goes missing — `search_code(query=…)` still returns results, just
worse ones, from a path the caller cannot see. It is the same shape as the four `runs`
columns that were written, returned, and read by nothing, which
`tests/integration/test_request_columns_are_read.py` was written to stop.

So this is that guard for the MCP surface, in two halves that fail differently:

**Classification** — every advertised tool is named in `REACHES` or in `TABLE_ONLY`, with a
reason. Adding a tool forces the choice, which is the point: the failure mode is not a
wrong decision but an absent one.

**Delivery** — each tool in `REACHES` is invoked with values that could not be confused
with a default, and the call the plane (or the workspace) received is checked for them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client

from contracts import ToolResult
from mcp_bridge import workspace
from tests.unit.test_mcp_server import RUN_ID, FakePlane, server, text_of

pytestmark = pytest.mark.unit

# Values chosen so that a dropped argument cannot pass by coinciding with a default. The
# four mutations that survived did so partly because a test had used `limit=20` against a
# default of `20` — a value equal to the default proves nothing about whether it travelled.
SENT: dict[str, dict[str, Any]] = {
    "create_run": {
        "repo_url": "https://github.com/acme/distinctive",
        "goal": "A goal no default could be mistaken for, long enough to pass validation.",
        "base_branch": "not-main",
        "budget_usd": 3.5,
        "unattended": True,
        "run_provider": "openai_compat",
    },
    "list_runs": {"limit": 7},
    "wait_for_run": {"run_id": str(RUN_ID), "timeout_s": 12.5},
    "answer_run": {"run_id": str(RUN_ID), "text": "the answer", "tool_call_id": "call-abc"},
    "approve_tool": {"run_id": str(RUN_ID), "tool_call_id": "call-approve"},
    "reject_tool": {
        "run_id": str(RUN_ID),
        "tool_call_id": "call-reject",
        "reason": "not on this repository",
    },
    "list_events": {"run_id": str(RUN_ID), "after_id": 41, "limit": 9},
    "get_artifact": {"run_id": str(RUN_ID), "kind": "diff"},
    "read_file": {
        "run_id": str(RUN_ID),
        "path": "fixture/ops.py",
        "start_line": 12,
        "end_line": 34,
    },
    "search_code": {
        "run_id": str(RUN_ID),
        "pattern": "def subtract",
        "query": "where is rate limiting handled",
        "glob": "*.py",
        "max_results": 3,
        "fixed_string": True,
    },
}

# What the plane must have been told. Each lambda gets the recorded (args, kwargs) of the
# plane call the tool is expected to make, and returns the values that must have arrived.
# Written out rather than derived, because a derivation would follow the same code this
# file exists to distrust.
REACHES: dict[str, tuple[str, Any]] = {
    "create_run": (
        "create_run",
        lambda a, k: (
            k["goal"],
            k["repo_url"],
            k["base_branch"],
            k["unattended"],
            k["budget"].max_usd,
            k["provider"],
        ),
    ),
    "list_runs": ("list_runs", lambda a, k: (a[0],)),
    "wait_for_run": ("wait_for_run", lambda a, k: (k["timeout_s"],)),
    "answer_run": ("answer", lambda a, k: (a[1], a[2])),
    "approve_tool": ("approve", lambda a, k: (a[1],)),
    "reject_tool": ("reject", lambda a, k: (a[1], a[2])),
    "list_events": ("list_events", lambda a, k: (k["after_id"], k["limit"])),
    "get_artifact": ("get_artifact", lambda a, k: (a[1],)),
}

EXPECTED: dict[str, tuple[Any, ...]] = {
    "create_run": (
        SENT["create_run"]["goal"],
        SENT["create_run"]["repo_url"],
        "not-main",
        True,
        3.5,
        "openai_compat",
    ),
    "list_runs": (7,),
    "wait_for_run": (12.5,),
    "answer_run": ("the answer", "call-abc"),
    "approve_tool": ("call-approve",),
    "reject_tool": ("call-reject", "not on this repository"),
    "list_events": (41, 9),
    "get_artifact": ("diff",),
}

# Tools whose arguments are only a run id, or whose delivery is asserted elsewhere. Each
# needs a reason, so that "I could not think of an assertion" cannot hide here.
TABLE_ONLY: dict[str, str] = {
    "get_run": "carries only the run id, and `_summary` is covered by test_mcp_server",
    "cancel_run": "carries only the run id",
    "list_artifacts": "carries only the run id",
    # These two do not reach the plane with their arguments — they reach `workspace`, and
    # the two tests at the bottom of this file assert that seam directly.
    "search_code": "asserted against the workspace seam, not the plane",
    "read_file": "asserted against the workspace seam, not the plane",
}


@dataclass
class FakeArtifact:
    kind: str = "diff"
    content: Any = field(default_factory=lambda: {"text": "--- a/x\n+++ b/x\n+fixed\n"})


def plane_for(tool: str) -> FakePlane:
    """A plane with whatever this tool needs to get as far as its arguments.

    `get_artifact` reads `.content` off what the plane returns, so it needs one — and it
    needs it for the *argument* assertion to be reachable at all. Setting it here keeps the
    delivery test about delivery rather than about fixture shape.
    """
    if tool == "get_artifact":
        return FakePlane(artifact=FakeArtifact())
    return FakePlane()


async def advertised(plane: FakePlane, worktrees: Path) -> set[str]:
    async with Client(server(plane, worktrees=worktrees), raise_exceptions=False) as client:
        return {tool.name for tool in (await client.list_tools()).tools}


async def test_every_advertised_tool_is_classified(tmp_path: Path) -> None:
    """Adding a tool forces a decision about whether its arguments are checked.

    The six mutations were not wrong decisions; they were absent ones. Nobody chose to
    leave `search_code`'s arguments unasserted — the tool was added, it appeared in the
    table test, and the question of whether its body ever ran was never asked.
    """
    names = await advertised(FakePlane(), tmp_path)
    unclassified = names - set(REACHES) - set(TABLE_ONLY)
    assert unclassified == set(), (
        f"new MCP tools {sorted(unclassified)}: add each to REACHES (with the plane call "
        "and the values that must arrive) or to TABLE_ONLY (with a reason)"
    )
    assert not (set(REACHES) & set(TABLE_ONLY)), "a tool cannot be both"


async def test_the_classification_covers_nothing_that_does_not_exist(tmp_path: Path) -> None:
    """The other direction: a renamed tool must not leave a stale entry asserting nothing.

    Without this, renaming `answer_run` would drop its delivery test silently — the
    parametrised test below would still pass on an entry nobody calls.
    """
    names = await advertised(FakePlane(), tmp_path)
    stale = (set(REACHES) | set(TABLE_ONLY)) - names
    assert stale == set(), f"classified but not advertised: {sorted(stale)}"


@pytest.mark.parametrize("tool", sorted(REACHES))
async def test_a_tools_arguments_reach_the_control_plane(tool: str, tmp_path: Path) -> None:
    """Parametrised so a failure names the tool rather than "the MCP server".

    "an argument did not arrive" is true of six separate surviving mutations; "`list_runs`
    dropped its limit" is the one that gets fixed.
    """
    plane = plane_for(tool)
    method, extract = REACHES[tool]

    async with Client(server(plane, worktrees=tmp_path), raise_exceptions=False) as client:
        result = await client.call_tool(tool, SENT[tool])

    assert not result.is_error, f"{tool} failed: {text_of(result)}"
    matching = [c for c in plane.calls if c[0] == method]
    assert matching, f"{tool} never called plane.{method}; recorded {[c[0] for c in plane.calls]}"
    _, args, kwargs = matching[0]
    assert extract(args, kwargs) == EXPECTED[tool]


# ---- the workspace seam ------------------------------------------------------------------
#
# `search_code` and `read_file` do not reach the plane with their arguments; they build a
# context and hand them to `mcp_bridge.workspace`. So the seam is there, and it is the one
# that hid the engine.


async def test_read_files_line_range_reaches_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A range the caller asked for, not the default. `start_line=1, end_line=-1` is the
    signature's own default, so asserting those would pass on a body that dropped both."""
    seen: dict[str, Any] = {}

    async def fake_read_file(ctx: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        seen["ctx"] = ctx
        return ToolResult(content="1: x", is_error=False)

    monkeypatch.setattr(workspace, "read_file", fake_read_file)
    plane = FakePlane()
    async with Client(server(plane, worktrees=tmp_path), raise_exceptions=False) as client:
        result = await client.call_tool("read_file", SENT["read_file"])

    assert not result.is_error, text_of(result)
    assert seen["path"] == "fixture/ops.py"
    assert (seen["start_line"], seen["end_line"]) == (12, 34)


async def test_search_codes_arguments_and_the_engine_reach_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine is the reason this file exists.

    `context_for` puts `plane.engine` on the context because semantic search looks the
    embedding index up by `base_sha`. With no engine the query still answers — from text
    search — so a run loses its index and says nothing about it. Passing `engine=None` at
    that call site survived the entire suite.

    `glob` is checked as `"*.py"` rather than the empty default because the body converts
    `glob or None`, and an assertion on the falsy default would hold for a dropped one too.
    """
    seen: dict[str, Any] = {}

    async def fake_search_code(ctx: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        seen["ctx"] = ctx
        return ToolResult(content="fixture/ops.py:3: def subtract", is_error=False)

    monkeypatch.setattr(workspace, "search_code", fake_search_code)
    sentinel = object()
    plane = FakePlane(engine=sentinel)

    async with Client(server(plane, worktrees=tmp_path), raise_exceptions=False) as client:
        result = await client.call_tool("search_code", SENT["search_code"])

    assert not result.is_error, text_of(result)
    assert seen["pattern"] == "def subtract"
    assert seen["query"] == "where is rate limiting handled"
    assert seen["glob"] == "*.py"
    assert seen["max_results"] == 3
    assert seen["fixed_string"] is True
    assert seen["ctx"].engine is sentinel, "no engine means semantic search degrades in silence"


async def test_the_context_carries_the_runs_real_branch_and_commit(tmp_path: Path) -> None:
    """`base_sha` is what the embedding index is keyed by, so an invented one is the same
    failure as a missing engine: an answer from the wrong place, with no error."""
    plane = FakePlane()
    ctx = workspace.context_for(plane.row, worktrees_dir=tmp_path, engine=None)
    assert ctx.base_sha == plane.row.base_sha
    assert ctx.work_branch == plane.row.work_branch
    assert ctx.run_id == RUN_ID
    assert isinstance(ctx.run_id, uuid.UUID)
