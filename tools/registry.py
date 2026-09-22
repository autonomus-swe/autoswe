"""All tools by name, and which roles may use which. Read-only roles are checked at import."""

from __future__ import annotations

from tools.ask_user import AskUserTool
from tools.base import BaseTool
from tools.bash import BashTool
from tools.editor import EditorTool
from tools.fs import ReadFileTool
from tools.git import GitCommitTool, GitDiffTool, GitLogTool, GitStatusTool
from tools.search import SearchCodeTool
from tools.symbols import ListSymbolsTool
from tools.tests import RunTestsTool

REGISTRY: dict[str, BaseTool] = {
    t.name: t
    for t in (
        BashTool(),
        EditorTool(),
        RunTestsTool(),
        ReadFileTool(),
        SearchCodeTool(),
        GitStatusTool(),
        GitDiffTool(),
        GitLogTool(),
        GitCommitTool(),
        AskUserTool(),
        ListSymbolsTool(),
    )
}

ROLE_TOOLS: dict[str, list[str]] = {
    "coder": [
        "bash",
        "str_replace_based_edit_tool",
        "read_file",
        "list_symbols",
        "search_code",
        "run_tests",
        "git_status",
        "git_diff",
        "git_commit",
        # last on purpose: a question costs a human's attention, so it is the
        # tool of last resort, and tool order nudges what a model reaches for.
        "ask_user",
    ],
    "debugger": [
        "bash",
        "str_replace_based_edit_tool",
        "read_file",
        "list_symbols",
        "search_code",
        "run_tests",
        "git_status",
        "git_diff",
        "git_commit",
        # last on purpose: a question costs a human's attention, so it is the
        # tool of last resort, and tool order nudges what a model reaches for.
        "ask_user",
    ],
    "analyzer": ["read_file", "list_symbols", "search_code", "git_status", "git_diff"],
    "planner": ["read_file", "list_symbols", "search_code", "git_status", "git_diff"],
    # The Reviewer reads history as well as the diff: "what did this run already try"
    # is a different question from "what does the diff say", and both inform a finding.
    "review": ["read_file", "list_symbols", "search_code", "git_status", "git_diff", "git_log"],
    "review_pre": ["read_file", "search_code", "git_status", "git_diff", "git_log"],
    # Read-only, and `bash` is deliberately absent even though a security reviewer would
    # find it convenient: this role is the one reading attacker-controlled strings out of a
    # diff, so it is the last one that should be able to run them.
    "security": ["read_file", "search_code", "git_status", "git_diff", "git_log"],
    "decomposer": [],
    "tester": [],
    # Empty on purpose, and the emptiness is the security property rather than an omission.
    # This role's output is published to a forge and copied into notification email, so a
    # writer that could open files could quote one — and the files it would most want to
    # quote are the ones a scanner just flagged. It is handed the facts instead
    # (`agents/pr_writer.py`), which is also why it needs no loop to gather them.
    "pr_writer": [],
}

READ_ONLY_ROLES = frozenset(
    {"analyzer", "planner", "review", "review_pre", "security", "pr_writer"}
)


def tools_for(role: str) -> list[BaseTool]:
    return [REGISTRY[name] for name in ROLE_TOOLS[role]]


def _check_read_only(role: str) -> None:
    """A read-only role has no mutating tools. Enforced by the tool layer, not the prompt
    (README §4.3)."""
    bad = [t.name for t in tools_for(role) if t.mutating]
    assert not bad, f"read-only role {role} has mutating tools: {bad}"


def register(tools: list[tuple[BaseTool, list[str]]]) -> None:
    """Add tools discovered at runtime — mounted MCP servers — to the registry.

    Registration is what makes a mounted tool subject to the harness rather than beside
    it. `orchestrator/hooks.py` looks a tool up here by name to read `requires_approval`
    and `mutating` off it; a tool the registry has never heard of is a `None` in that
    lookup, and a `None` is approved by nobody and audited as nothing.

    The read-only-role rule is re-checked over the result, not just over the additions. A
    mutating tool added to `analyzer` fails here even if it slipped past
    `mcp_bridge.config`, and it fails at startup rather than the first time an analyzer
    decides to use it.

    Rejections leave the registry as it was. A half-applied mount is worse than none:
    the process would carry some of the tools the configuration named and none of the
    reason why, which is the state hardest to diagnose from a log.

    Both containers are mutated **in place**, and that is load-bearing rather than a
    style. Each agent holds its role's list by reference — `tool_names: ClassVar[list[str]]
    = ROLE_TOOLS["coder"]`, bound at import — so an append reaches agents that were
    defined before this ran, and rebinding `ROLE_TOOLS[role]` to a new list would reach
    none of them. `tests/unit/test_mcp_registry.py` pins that a mounted tool arrives at
    its agent, because the failure is silent: the configuration parses, the server
    connects, the tool registers, and no model is ever offered it.
    """
    if clash := [t.name for t, _ in tools if t.name in REGISTRY]:
        raise ValueError(f"tools already registered: {clash}")
    if unknown := sorted({r for _, roles in tools for r in roles if r not in ROLE_TOOLS}):
        raise ValueError(f"unknown roles: {unknown}")

    added_names = {t.name for t, _ in tools}
    for tool, roles in tools:
        REGISTRY[tool.name] = tool
        for role in roles:
            ROLE_TOOLS[role].append(tool.name)
    try:
        for role in READ_ONLY_ROLES:
            _check_read_only(role)
    except AssertionError:
        for name in added_names:
            REGISTRY.pop(name, None)
        for names in ROLE_TOOLS.values():
            names[:] = [n for n in names if n not in added_names]
        raise


for _role in READ_ONLY_ROLES:
    _check_read_only(_role)
