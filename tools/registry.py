"""All tools by name, and which roles may use which. Read-only roles are checked at import."""

from __future__ import annotations

from tools.base import BaseTool
from tools.bash import BashTool
from tools.editor import EditorTool
from tools.fs import ReadFileTool
from tools.git import GitCommitTool, GitDiffTool, GitStatusTool
from tools.search import SearchCodeTool
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
        GitCommitTool(),
    )
}

ROLE_TOOLS: dict[str, list[str]] = {
    "coder": [
        "bash",
        "str_replace_based_edit_tool",
        "read_file",
        "search_code",
        "run_tests",
        "git_status",
        "git_diff",
        "git_commit",
    ],
    "debugger": [
        "bash",
        "str_replace_based_edit_tool",
        "read_file",
        "search_code",
        "run_tests",
        "git_status",
        "git_diff",
        "git_commit",
    ],
    "analyzer": ["read_file", "search_code", "git_status", "git_diff"],
    "planner": ["read_file", "search_code", "git_status", "git_diff"],
    "review": ["read_file", "search_code", "git_status", "git_diff"],
    "review_pre": ["read_file", "search_code", "git_status", "git_diff"],
    "decomposer": [],
    "tester": [],
    "pr_writer": ["read_file", "search_code", "git_status", "git_diff"],
}

READ_ONLY_ROLES = frozenset({"analyzer", "planner", "review", "review_pre", "pr_writer"})


def tools_for(role: str) -> list[BaseTool]:
    return [REGISTRY[name] for name in ROLE_TOOLS[role]]


for _role in READ_ONLY_ROLES:  # enforced by the tool layer, not the prompt (README §4.3)
    _bad = [t.name for t in tools_for(_role) if t.mutating]
    assert not _bad, f"read-only role {_role} has mutating tools: {_bad}"
