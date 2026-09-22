"""The MCP server's shape conversion, against a control plane that records what it got.

These do not check that approving a run works — `api.service` owns that and the
integration tests exercise it through both transports. What they check is everything the
MCP layer adds on top: that arguments arrive as the service's parameters, that a refusal
reaches the client with its text intact, and that a tool a client can see does something
other than crash.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.client import Client

from api.service import Conflict, ControlPlane, NotFound
from mcp_bridge.server import build_server

pytestmark = pytest.mark.unit

RUN_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


@dataclass
class FakeRow:
    id: uuid.UUID = RUN_ID
    status: str = "running"
    phase: str = "code"
    goal: str = "Implement subtract(a, b)"
    repo_url: str = "https://github.com/acme/demo"
    work_branch: str = f"agent/{RUN_ID}"
    cost_usd: float = 1.25
    pr_url: str | None = None
    error: str | None = None
    base_sha: str | None = "abc123"
    updated_at: datetime = field(default_factory=lambda: datetime(2026, 9, 22, tzinfo=UTC))


@dataclass
class FakePlane:
    """Records calls and returns whatever the test set, including exceptions to raise."""

    engine: Any = None
    row: FakeRow = field(default_factory=FakeRow)
    raises: Exception | None = None
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    artifact: Any = None
    events: list[Any] = field(default_factory=list)
    pending: dict[str, Any] | None = None

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.raises is not None:
            raise self.raises

    async def create_run(self, **kwargs: Any) -> uuid.UUID:
        self._record("create_run", **kwargs)
        return RUN_ID

    async def get_run(self, run_id: uuid.UUID) -> FakeRow:
        self._record("get_run", run_id)
        return self.row

    async def list_runs(self, limit: int = 50) -> list[FakeRow]:
        self._record("list_runs", limit)
        return [self.row]

    async def wait_for_run(self, run_id: uuid.UUID, *, timeout_s: float = 1800.0) -> FakeRow:
        self._record("wait_for_run", run_id, timeout_s=timeout_s)
        return self.row

    async def pending_question(self, run_id: uuid.UUID) -> dict[str, Any] | None:
        self._record("pending_question", run_id)
        return self.pending

    async def answer(self, run_id: uuid.UUID, text: str, tool_call_id: str | None = None) -> None:
        self._record("answer", run_id, text, tool_call_id)

    async def approve(self, run_id: uuid.UUID, tool_call_id: str) -> None:
        self._record("approve", run_id, tool_call_id)

    async def reject(self, run_id: uuid.UUID, tool_call_id: str, reason: str) -> None:
        self._record("reject", run_id, tool_call_id, reason)

    async def cancel(self, run_id: uuid.UUID) -> None:
        self._record("cancel", run_id)

    async def list_events(
        self, run_id: uuid.UUID, after_id: int = 0, limit: int = 200
    ) -> list[Any]:
        self._record("list_events", run_id, after_id=after_id, limit=limit)
        return self.events

    async def list_artifacts(self, run_id: uuid.UUID) -> list[Any]:
        self._record("list_artifacts", run_id)
        return []

    async def get_artifact(self, run_id: uuid.UUID, kind: str) -> Any:
        self._record("get_artifact", run_id, kind)
        return self.artifact


def server(
    plane: FakePlane, *, worktrees: Path | None = None, provider: str = "openai_compat"
) -> Any:
    # A stand-in with the same methods rather than a real `ControlPlane`: what these tests
    # check is the conversion on either side of it, so the plane is the seam.
    return build_server(
        lambda: cast(ControlPlane, plane), provider=provider, worktrees_dir=worktrees
    )


def text_of(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


async def test_every_documented_tool_is_registered() -> None:
    """The plan's tool table, checked as a set rather than one assertion per tool.

    A tool that exists but is not advertised is invisible; one advertised but absent is
    worse, because the caller finds out by calling it.
    """
    plane = FakePlane()
    async with Client(server(plane, worktrees=Path("/tmp")), raise_exceptions=False) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {
        "create_run",
        "get_run",
        "list_runs",
        "wait_for_run",
        "answer_run",
        "approve_tool",
        "reject_tool",
        "cancel_run",
        "list_events",
        "list_artifacts",
        "get_artifact",
        "search_code",
        "read_file",
    }


async def test_workspace_tools_are_absent_without_a_worktrees_dir() -> None:
    """A deployment where the API cannot see the worker's disk advertises neither.

    Registering them anyway would put two tools in front of the model that fail every
    time, and a model that has been told a tool exists will keep trying it.
    """
    async with Client(server(FakePlane()), raise_exceptions=False) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "search_code" not in names and "read_file" not in names


async def test_create_run_passes_its_arguments_through() -> None:
    plane = FakePlane()
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool(
            "create_run",
            {
                "repo_url": "https://github.com/acme/demo",
                "goal": "Implement subtract(a, b)",
                "base_branch": "develop",
                "budget_usd": 3.0,
                "unattended": True,
            },
        )
    assert not result.is_error
    assert result.structured_content == {"run_id": str(RUN_ID)}
    (name, _, kwargs) = plane.calls[0]
    assert name == "create_run"
    assert kwargs["repo_url"] == "https://github.com/acme/demo"
    assert kwargs["base_branch"] == "develop"
    assert kwargs["unattended"] is True
    assert kwargs["budget"].max_usd == 3.0
    # The deployment's default, since this call named none.
    assert kwargs["provider"] == "openai_compat"


async def test_create_run_can_name_a_provider() -> None:
    """Step 6.3 taught the worker to build from the run's row, so this argument now
    decides something. Before that it would have been recorded and ignored.

    The server's own default is set to something else on purpose: with both the same, a
    tool that ignored the argument would still record the right value, and a mutation that
    did exactly that survived the first version of this.
    """
    plane = FakePlane()
    async with Client(server(plane, provider="anthropic"), raise_exceptions=False) as client:
        result = await client.call_tool(
            "create_run",
            {
                "repo_url": "https://github.com/acme/demo",
                "goal": "Implement subtract(a, b)",
                "run_provider": "openai_compat",
            },
        )
    assert not result.is_error, text_of(result)
    assert plane.calls[0][2]["provider"] == "openai_compat"


async def test_a_provider_this_build_cannot_make_is_refused_before_the_run_exists() -> None:
    """Otherwise the run is created, queued, and fails in a worker the caller cannot see."""
    plane = FakePlane()
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool(
            "create_run",
            {
                "repo_url": "https://github.com/acme/demo",
                "goal": "Implement subtract(a, b)",
                "run_provider": "anthropic",
            },
        )
    assert result.is_error and "openai_compat" in text_of(result)
    assert plane.calls == []


async def test_a_refused_budget_is_reported_not_recorded() -> None:
    plane = FakePlane()
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool(
            "create_run",
            {"repo_url": "https://github.com/acme/demo", "goal": "x" * 20, "budget_usd": 0},
        )
    assert result.is_error and "budget_usd" in text_of(result)
    assert plane.calls == []


async def test_a_malformed_run_id_says_so() -> None:
    """Rather than reaching the service and failing there as something less specific."""
    plane = FakePlane()
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool("get_run", {"run_id": "run-42"})
    assert result.is_error and "not a run id" in text_of(result)
    assert plane.calls == []


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (NotFound("run not found"), "run not found"),
        (Conflict("the run is waiting on toolu_9, not toolu_1"), "waiting on toolu_9"),
    ],
)
async def test_a_control_error_keeps_its_text(error: Exception, fragment: str) -> None:
    """The SDK discards a crashing tool's message and sends "Error executing tool X".

    That is right for a crash and wrong for these: "the run is waiting on a different
    call" is the answer, and a caller that receives only the tool's name cannot act on it.
    """
    plane = FakePlane(raises=error)
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool(
            "approve_tool", {"run_id": str(RUN_ID), "tool_call_id": "toolu_1"}
        )
    assert result.is_error and fragment in text_of(result)


async def test_an_unexpected_failure_does_not_leak_its_message() -> None:
    """The other half of the same rule: a crash is not a message for the caller."""
    plane = FakePlane(raises=RuntimeError("connection string: postgres://user:hunter2@db"))
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool("cancel_run", {"run_id": str(RUN_ID)})
    assert result.is_error
    assert "hunter2" not in text_of(result)


async def test_an_empty_tool_call_id_is_sent_as_none() -> None:
    """MCP schemas have no null for a plain string, so the tool takes "" and converts.

    The service treats `None` and `""` differently — an answer with a tool call id is
    matched against the call the run is parked on, one without is for the planner's open
    questions — so passing the empty string straight through would address every planner
    answer to a tool call named "".
    """
    plane = FakePlane()
    async with Client(server(plane), raise_exceptions=False) as client:
        await client.call_tool("answer_run", {"run_id": str(RUN_ID), "text": "yes, Postgres"})
    assert plane.calls == [("answer", (RUN_ID, "yes, Postgres", None), {})]


async def test_a_parked_run_comes_back_with_the_question() -> None:
    """`wait_for_run` returning `awaiting_input` without saying what it wants would make
    the caller poll events to find out, which is the work this tool exists to avoid."""
    plane = FakePlane(
        row=FakeRow(status="awaiting_input"),
        pending={"kind": "approval", "tool_call_id": "toolu_7", "tool_name": "bash"},
    )
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool("wait_for_run", {"run_id": str(RUN_ID), "timeout_s": 5.0})
    body = result.structured_content or {}
    assert body["status"] == "awaiting_input"
    assert body["pending"]["tool_call_id"] == "toolu_7"
    assert ("wait_for_run", (RUN_ID,), {"timeout_s": 5.0}) in plane.calls


async def test_a_running_run_is_not_asked_what_it_is_waiting_for() -> None:
    """One query per poll, not two, for a field that is null except when parked."""
    plane = FakePlane(row=FakeRow(status="running"))
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool("get_run", {"run_id": str(RUN_ID)})
    assert "pending" not in (result.structured_content or {})
    assert not any(name == "pending_question" for name, _, _ in plane.calls)


class FakeArtifact:
    def __init__(self, content: Any) -> None:
        self.content = content


async def test_a_diff_artifact_comes_back_as_a_diff() -> None:
    """`diff` is stored as `{"text": …}`; JSON-encoding it would escape every newline and
    hand the caller something no `git apply` will read."""
    plane = FakePlane(artifact=FakeArtifact({"text": "--- a/x.py\n+++ b/x.py\n+pass\n"}))
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool("get_artifact", {"run_id": str(RUN_ID), "kind": "diff"})
    assert text_of(result).startswith("--- a/x.py\n+++ b/x.py")


async def test_a_structured_artifact_comes_back_as_json() -> None:
    plane = FakePlane(artifact=FakeArtifact({"passed": 12, "failed": 0}))
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool(
            "get_artifact", {"run_id": str(RUN_ID), "kind": "test_report"}
        )
    assert '"passed": 12' in text_of(result)


async def test_a_missing_artifact_is_not_found_rather_than_empty() -> None:
    plane = FakePlane(raises=NotFound("run has no 'diff' artifact"))
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool("get_artifact", {"run_id": str(RUN_ID), "kind": "diff"})
    assert result.is_error and "no 'diff' artifact" in text_of(result)


class FakeEvent:
    def __init__(self, id: int) -> None:
        self.id = id
        self.type = "log"
        self.payload = {"message": f"event {id}"}
        self.ts = datetime(2026, 9, 22, tzinfo=UTC)


async def test_events_carry_a_cursor_for_the_next_page() -> None:
    plane = FakePlane(events=[FakeEvent(7), FakeEvent(9)])
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool("list_events", {"run_id": str(RUN_ID), "after_id": 3})
    body = result.structured_content or {}
    assert [e["id"] for e in body["events"]] == [7, 9]
    assert body["next_after_id"] == 9


async def test_an_empty_page_leaves_the_cursor_where_it_was() -> None:
    """Returning 0 would send a caller that had read 400 events back to the beginning."""
    plane = FakePlane(events=[])
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.call_tool("list_events", {"run_id": str(RUN_ID), "after_id": 400})
    assert (result.structured_content or {})["next_after_id"] == 400


async def test_the_artifact_resource_reads_the_same_artifact() -> None:
    plane = FakePlane(artifact=FakeArtifact({"text": "diff body"}))
    async with Client(server(plane), raise_exceptions=False) as client:
        result = await client.read_resource(f"run://{RUN_ID}/artifacts/diff")
    assert "diff body" in "".join(getattr(c, "text", "") for c in result.contents)
