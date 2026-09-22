"""Adding tools to the registry at runtime.

Registration is what puts a mounted tool inside the harness rather than beside it:
`orchestrator/hooks.py` looks a name up here to decide whether a human has to approve the
call. So the rules the registry enforces at import — a read-only role has no mutating
tools — have to survive tools arriving after it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

from contracts import ToolResult
from tools import registry
from tools.base import BaseTool, RunContext, schema

pytestmark = pytest.mark.unit


class Fake(BaseTool):
    name: ClassVar[str] = "fake"
    description: ClassVar[str] = "a tool"
    input_schema: ClassVar[dict[str, Any]] = schema({})

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        return ToolResult(content="ok")


def make(name: str, *, mutating: bool) -> BaseTool:
    cls = type(
        f"Fake_{name}",
        (Fake,),
        {"name": name, "mutating": mutating, "requires_approval": mutating},
    )
    return cls()  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def _restore() -> Iterator[None]:
    """The registry is module state. Every test here mutates it, so every test puts it
    back — otherwise the first failure makes the rest of the suite lie."""
    tools = dict(registry.REGISTRY)
    roles = {role: list(names) for role, names in registry.ROLE_TOOLS.items()}
    yield
    registry.REGISTRY.clear()
    registry.REGISTRY.update(tools)
    for role, names in roles.items():
        registry.ROLE_TOOLS[role][:] = names


def test_a_registered_tool_is_reachable_by_name_and_by_role() -> None:
    registry.register([(make("mcp_stub_echo", mutating=False), ["coder", "analyzer"])])
    assert registry.REGISTRY["mcp_stub_echo"].name == "mcp_stub_echo"
    assert "mcp_stub_echo" in [t.name for t in registry.tools_for("coder")]
    assert "mcp_stub_echo" in [t.name for t in registry.tools_for("analyzer")]
    assert "mcp_stub_echo" not in [t.name for t in registry.tools_for("security")]


def test_a_mutating_tool_cannot_be_added_to_a_read_only_role() -> None:
    """The same rule the module asserts at import, applied to what arrives later.

    `mcp_bridge.config` refuses this too, with a better message. Both exist on purpose:
    that one is early and legible, this one cannot be bypassed by any route that reaches
    the registry.
    """
    with pytest.raises(AssertionError, match="read-only role"):
        registry.register([(make("mcp_gh_comment", mutating=True), ["analyzer"])])


def test_a_rejected_registration_leaves_nothing_behind() -> None:
    """A half-applied mount is worse than none: the process carries some of the tools the
    configuration named and no sign of why the rest are missing."""
    before_registry = dict(registry.REGISTRY)
    before_roles = {r: list(n) for r, n in registry.ROLE_TOOLS.items()}
    with pytest.raises(AssertionError):
        registry.register(
            [
                (make("mcp_gh_read", mutating=False), ["analyzer"]),
                (make("mcp_gh_comment", mutating=True), ["analyzer"]),
            ]
        )
    assert registry.REGISTRY == before_registry
    assert {r: list(n) for r, n in registry.ROLE_TOOLS.items()} == before_roles


def test_a_name_that_is_already_taken_is_refused() -> None:
    """The prefix makes this unlikely, and unlikely is not never — two servers both named
    `github` in one file, or a second mount after a reconnect. Shadowing `bash` silently
    is not a failure mode worth leaving open."""
    with pytest.raises(ValueError, match="already registered"):
        registry.register([(make("bash", mutating=False), ["coder"])])


def test_an_unknown_role_is_refused_before_anything_is_added() -> None:
    with pytest.raises(ValueError, match="unknown roles"):
        registry.register([(make("mcp_stub_echo", mutating=False), ["codr"])])
    assert "mcp_stub_echo" not in registry.REGISTRY


def test_read_only_roles_still_hold_after_a_legitimate_mount() -> None:
    """The assertion is re-run over the whole registry, not just the additions."""
    registry.register(
        [
            (make("mcp_pg_query", mutating=False), ["analyzer"]),
            (make("mcp_gh_comment", mutating=True), ["coder"]),
        ]
    )
    for role in registry.READ_ONLY_ROLES:
        assert [t.name for t in registry.tools_for(role) if t.mutating] == []


def test_a_registered_tool_reaches_the_agent_that_should_have_it() -> None:
    """The end of the wire, and the reason `register` mutates in place.

    Each agent binds its role's list at import — `tool_names = ROLE_TOOLS["coder"]` — so
    an append reaches agents defined long before the mount, and rebinding the key to a new
    list would reach none of them. That failure is silent in every visible way: the file
    parses, the server connects, the tool registers, and no model is ever offered it.
    """
    from agents.analyzer import AnalyzerAgent
    from agents.coder import CoderAgent

    registry.register([(make("mcp_gh_get_issue", mutating=False), ["coder"])])
    assert "mcp_gh_get_issue" in [t.name for t in CoderAgent().tools()]
    assert "mcp_gh_get_issue" not in [t.name for t in AnalyzerAgent().tools()]
