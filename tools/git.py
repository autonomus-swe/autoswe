"""Host-side git tools. The sandbox has no usable git, so the model goes through these."""

from __future__ import annotations

import re
from typing import Any

from contracts import ToolResult
from core.errors import RepoError
from repo.gitcmd import git
from tools.base import BaseTool, RunContext, schema

COMMIT_RE = re.compile(r"^(feat|fix|refactor|test|chore|docs)(\(.+\))?: .+")
DIFF_CAP = 200_000


class GitStatusTool(BaseTool):
    name = "git_status"
    description = "Show changed files in the workspace (git status --porcelain)."
    input_schema = schema({})
    mutating = False
    parallel_safe = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        out = await git("status", "--porcelain", cwd=ctx.worktree)
        return ToolResult(content=out.strip() or "(clean: no changes)")


class GitDiffTool(BaseTool):
    name = "git_diff"
    description = "Show the diff of the workspace against the base commit."
    input_schema = schema({})
    mutating = False
    parallel_safe = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        out = await git("diff", ctx.base_sha, cwd=ctx.worktree)
        if len(out) > DIFF_CAP:
            out = out[:DIFF_CAP] + f"\n…[diff truncated at {DIFF_CAP} bytes]…\n"
        return ToolResult(content=out or "(no diff)")


class GitLogTool(BaseTool):
    name = "git_log"
    description = (
        "List the commits this run has made, newest first, as '<short sha> <subject>'. "
        "Use it to see what has already been done before adding to it."
    )
    input_schema = schema({})
    mutating = False
    parallel_safe = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        out = await git("log", f"{ctx.base_sha}..HEAD", "--format=%h %s", cwd=ctx.worktree)
        return ToolResult(content=out.strip() or "(no commits on this branch yet)")


class GitCommitTool(BaseTool):
    name = "git_commit"
    description = (
        "Stage all changes and commit them on the run branch. The message must be a conventional "
        "commit: '<type>(<scope>)?: <summary>' with type in feat, fix, refactor, test, chore, docs."
    )
    input_schema = schema({"message": {"type": "string"}}, required=["message"])
    mutating = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        message = str(kwargs.get("message", "")).strip()
        if not COMMIT_RE.match(message):
            return ToolResult(
                content="error: message must match '<type>(<scope>)?: <summary>' with type in "
                "feat|fix|refactor|test|chore|docs, e.g. 'feat(ops): add subtract'",
                is_error=True,
            )
        branch = (await git("rev-parse", "--abbrev-ref", "HEAD", cwd=ctx.worktree)).strip()
        if branch != ctx.work_branch:
            raise RepoError(f"refusing to commit on {branch!r}; expected {ctx.work_branch!r}")
        await git("add", "-A", cwd=ctx.worktree)
        staged = (await git("diff", "--cached", "--name-only", cwd=ctx.worktree)).split()
        if not staged:
            return ToolResult(content="error: nothing to commit", is_error=True)
        await git("commit", "-q", "-m", message, cwd=ctx.worktree)
        sha = (await git("rev-parse", "HEAD", cwd=ctx.worktree)).strip()
        return ToolResult(content=f"committed {sha[:12]}: {message} ({len(staged)} files)")
