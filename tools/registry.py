"""All tools by name, and which roles may use which. Read-only roles are checked at import."""

from __future__ import annotations

from tools.ask_user import AskUserTool
from tools.base import BaseTool
from tools.bash import BashTool
from tools.editor import EditorTool
from tools.fs import ReadFileTool
from tools.git import GitCommitTool, GitDiffTool, GitLogTool, GitStatusTool
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
        GitLogTool(),
        GitCommitTool(),
        AskUserTool(),
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
        # last on purpose: a question costs a human's attention, so it is the
        # tool of last resort, and tool order nudges what a model reaches for.
        "ask_user",
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
        # last on purpose: a question costs a human's attention, so it is the
        # tool of last resort, and tool order nudges what a model reaches for.
        "ask_user",
    ],
    "analyzer": ["read_file", "search_code", "git_status", "git_diff"],
    "planner": ["read_file", "search_code", "git_status", "git_diff"],
    # The Reviewer reads history as well as the diff: "what did this run already try"
    # is a different question from "what does the diff say", and both inform a finding.
    "review": ["read_file", "search_code", "git_status", "git_diff", "git_log"],
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


for _role in READ_ONLY_ROLES:  # enforced by the tool layer, not the prompt (README §4.3)
    _bad = [t.name for t in tools_for(_role) if t.mutating]
    assert not _bad, f"read-only role {_role} has mutating tools: {_bad}"
