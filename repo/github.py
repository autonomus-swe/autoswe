"""Push the run branch and open (or find) its pull request."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

from core.errors import RepoError
from repo.gitcmd import git, git_auth_env

_URL = re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    m = _URL.match(repo_url.strip())
    if not m:
        raise RepoError(f"not a github.com repository URL: {repo_url}")
    return m["owner"], m["name"]


async def push_branch(worktree: Path, branch: str, token: str) -> None:
    """``git push origin <branch>`` with per-process credentials. Never forces, never main."""
    if not branch.startswith("agent/"):
        raise RepoError(f"refusing to push non-agent branch {branch!r}")
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    await git("push", "origin", refspec, cwd=worktree, env=git_auth_env(token))


class GitHubClient(Protocol):
    """The slice of PyGithub we use, so tests and the local e2e can substitute it."""

    def get_repo(self, full_name: str) -> Any: ...


def pygithub_client(token: str) -> GitHubClient:
    from github import Auth, Github

    return Github(auth=Auth.Token(token))


async def open_pr(
    repo_url: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
    client: GitHubClient,
    draft: bool = False,
) -> str:
    """Return the PR URL, reusing an open PR for ``head`` if one exists (idempotent)."""
    import asyncio

    owner, name = parse_repo_url(repo_url)

    def _open() -> str:
        repo = client.get_repo(f"{owner}/{name}")
        for pr in repo.get_pulls(state="open", head=f"{owner}:{head}"):
            return str(pr.html_url)
        pr = repo.create_pull(title=title, body=body, head=head, base=base, draft=draft)
        return str(pr.html_url)

    return await asyncio.to_thread(_open)


def pr_body(*, goal: str, diff_stat: str, test_summary: str, run_id: str) -> str:
    return (
        f"## Goal\n\n{goal}\n\n## Changes\n\n```\n{diff_stat.strip()}\n```\n\n"
        f"## Tests\n\n{test_summary}\n\n---\nOpened by autoswe run `{run_id}`\n"
    )
