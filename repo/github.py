"""Push the run branch and open (or find) its pull request."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

from contracts import SecurityFinding
from core.errors import RepoError
from repo.gitcmd import git, git_auth_env


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Split a repository location into ``(owner, name)``.

    Which hosts are allowed is a control-plane policy (``api.schemas.RunCreate``), not a
    git-layer one, so this accepts any location whose last two segments are the owner and
    the repository: an https URL, an ssh remote, or a local path used by tests.
    """
    cleaned = re.sub(r"^[a-z][a-z0-9+.-]*://", "", repo_url.strip().rstrip("/"), flags=re.I)
    cleaned = re.sub(r"^[^@/]+@", "", cleaned)  # ssh user, e.g. git@github.com:owner/name
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    segments = [part for part in re.split(r"[/:]", cleaned) if part]
    if len(segments) < 2:
        raise RepoError(f"cannot read owner/name from repository location: {repo_url}")
    return segments[-2], segments[-1]


def gitleaks_gate(findings: list[SecurityFinding]) -> list[SecurityFinding]:
    """The findings that must stop a push, before anything leaves this machine.

    A secret is the one finding that gets worse the moment the branch is pushed: a force
    push does not remove it from a forge that has already indexed it, and a pull request
    body would carry it into a notification email. So this gate is not "block the run" like
    the others — it is "do not transmit", and it runs before `push_branch`.

    Every gitleaks finding counts, not only the ones tagged `in_diff`. gitleaks is already
    scoped with ``--log-opts base..HEAD``, so anything it reports is in a commit this run
    made; requiring the diff tag as well would mean a line-mapping miss could let a real
    secret through, and that is the wrong direction to be wrong in. Failures to *run* are
    `scan-failed` and do not block a push — a scanner that did not run has found nothing,
    and refusing every push on a broken scanner would make the tool impossible to keep.
    """
    return [f for f in findings if f.tool == "gitleaks" and f.rule != "scan-failed"]


async def push_branch(worktree: Path, branch: str, token: str | None = None) -> None:
    """``git push origin <branch>``. Never forces, never a non-agent branch.

    ``token`` is only needed for remotes that authenticate (github.com over https); a
    local path or an already-authenticated remote works without one.
    """
    if not branch.startswith("agent/"):
        raise RepoError(f"refusing to push non-agent branch {branch!r}")
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    await git("push", "origin", refspec, cwd=worktree, env=git_auth_env(token) if token else None)


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

    try:
        return await asyncio.to_thread(_open)
    except Exception as e:  # a TLS failure here is almost always a proxy CA, not a bug
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        raise RepoError(
            "TLS verification failed talking to the GitHub API. A TLS-inspecting proxy is "
            "signing this connection with a root CA that Python does not trust. Set "
            "CA_BUNDLE in .env to your system bundle (usually "
            "/etc/ssl/certs/ca-certificates.crt); git already trusts it."
        ) from e


def pr_body(*, goal: str, diff_stat: str, test_summary: str, run_id: str) -> str:
    return (
        f"## Goal\n\n{goal}\n\n## Changes\n\n```\n{diff_stat.strip()}\n```\n\n"
        f"## Tests\n\n{test_summary}\n\n---\nOpened by autoswe run `{run_id}`\n"
    )
