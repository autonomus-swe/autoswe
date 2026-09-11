"""Bare clone cache: one bare repo per URL under REPOS_DIR, refreshed on every run."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from repo.gitcmd import git, git_auth_env

# Where per-run branches live. ``repo.worktree.branch_for`` builds names under this.
AGENT_NAMESPACE = "agent"


def repo_key(repo_url: str) -> str:
    return hashlib.sha1(repo_url.strip().lower().encode()).hexdigest()  # noqa: S324 (not security)


def _auth(repo_url: str, token: str | None) -> dict[str, str]:
    return git_auth_env(token) if token and repo_url.startswith("https://github.com/") else {}


async def ensure_bare_clone(repo_url: str, repos_dir: Path, token: str | None = None) -> Path:
    """Clone on first use, fetch afterwards. Returns the bare repo path."""
    await asyncio.to_thread(repos_dir.mkdir, parents=True, exist_ok=True)
    bare = repos_dir / f"{repo_key(repo_url)}.git"
    env = _auth(repo_url, token)
    if not await asyncio.to_thread(bare.exists):
        await git("clone", "--bare", "--quiet", repo_url, str(bare), env=env)
    else:
        # A resumed run's branch exists here but not on origin yet, so an unqualified
        # --prune deletes it and orphans the worktree on an unborn branch — the run then
        # loses every commit it had made. Exclude the agent namespace from the refspec;
        # branches genuinely deleted upstream are still pruned.
        await git(
            "fetch",
            "--prune",
            "--quiet",
            "origin",
            "+refs/heads/*:refs/heads/*",
            f"^refs/heads/{AGENT_NAMESPACE}/*",
            cwd=bare,
            env=env,
        )
    return bare


async def resolve_sha(bare: Path, branch: str) -> str:
    return (await git("rev-parse", f"refs/heads/{branch}", cwd=bare)).strip()
