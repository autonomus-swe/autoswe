"""Worktree lifecycle against a local bare fixture, plus GitHub helpers with a fake client."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.errors import RepoError
from repo import worktree
from repo.clone import ensure_bare_clone, repo_key, resolve_sha
from repo.gitcmd import git, git_auth_env
from repo.github import open_pr, parse_repo_url, pr_body, push_branch

pytestmark = pytest.mark.unit


async def make_origin(tmp_path: Path) -> Path:
    """A non-bare 'upstream' repo with one commit on main, used as the clone source."""
    src = tmp_path / "origin"
    src.mkdir()
    await git("init", "-q", "-b", "main", cwd=src)
    (src / "README.md").write_text("hello\n")
    await git("add", "-A", cwd=src)
    await git("commit", "-q", "-m", "init", cwd=src)
    return src


async def test_bare_clone_then_fetch_and_worktree_roundtrip(tmp_path: Path) -> None:
    src = await make_origin(tmp_path)
    repos = tmp_path / "repos"
    bare = await ensure_bare_clone(str(src), repos)
    assert bare == repos / f"{repo_key(str(src))}.git" and (bare / "HEAD").exists()
    sha = await resolve_sha(bare, "main")
    assert len(sha) == 40

    # second call fetches instead of cloning and picks up new upstream commits
    (src / "b.txt").write_text("b\n")
    await git("add", "-A", cwd=src)
    await git("commit", "-q", "-m", "second", cwd=src)
    assert await ensure_bare_clone(str(src), repos) == bare
    assert await resolve_sha(bare, "main") != sha

    wt = await worktree.create(bare, tmp_path / "wts", "abc-123", "main")
    assert wt.branch == "agent/abc-123" and (wt.path / "README.md").exists()
    assert (await git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt.path)).strip() == "agent/abc-123"
    common = Path((await git("rev-parse", "--git-common-dir", cwd=wt.path)).strip())
    assert ".venv/" in (common / "info" / "exclude").read_text()

    # .venv is ignored by add -A thanks to the exclude file
    (wt.path / ".venv").mkdir()
    (wt.path / ".venv" / "x").write_text("x")
    (wt.path / "new.py").write_text("print(1)\n")
    await git("add", "-A", cwd=wt.path)
    staged = (await git("diff", "--cached", "--name-only", cwd=wt.path)).split()
    assert staged == ["new.py"]
    await git("commit", "-q", "-m", "feat: add", cwd=wt.path)
    author = (await git("log", "-1", "--format=%an <%ae>", cwd=wt.path)).strip()
    assert author == "autoswe[bot] <autoswe@users.noreply.github.com>"

    await worktree.remove(wt)
    assert not wt.path.exists()
    branches = (await git("branch", "--list", "agent/abc-123", cwd=bare)).strip()
    assert branches == ""


def test_git_auth_env_never_contains_plain_token() -> None:
    env = git_auth_env("ghp_secret")
    assert "ghp_secret" not in "".join(env.values()) and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")


def test_parse_repo_url() -> None:
    assert parse_repo_url("https://github.com/acme/demo") == ("acme", "demo")
    assert parse_repo_url("https://github.com/acme/demo.git/") == ("acme", "demo")
    with pytest.raises(RepoError):
        parse_repo_url("https://gitlab.com/acme/demo")


async def test_push_branch_refuses_non_agent_branch(tmp_path: Path) -> None:
    with pytest.raises(RepoError, match="non-agent"):
        await push_branch(tmp_path, "main", "tok")


class _PR:
    def __init__(self, url: str) -> None:
        self.html_url = url


class _Repo:
    def __init__(self, existing: list[_PR]) -> None:
        self.existing = existing
        self.created: list[dict[str, Any]] = []

    def get_pulls(self, **kw: Any) -> list[_PR]:
        return self.existing

    def create_pull(self, **kw: Any) -> _PR:
        self.created.append(kw)
        return _PR("https://github.com/acme/demo/pull/2")


class _Client:
    def __init__(self, repo: _Repo) -> None:
        self.repo = repo
        self.asked: list[str] = []

    def get_repo(self, full_name: str) -> _Repo:
        self.asked.append(full_name)
        return self.repo


async def test_open_pr_is_idempotent() -> None:
    repo = _Repo([_PR("https://github.com/acme/demo/pull/1")])
    client = _Client(repo)
    url = await open_pr(
        "https://github.com/acme/demo",
        head="agent/x",
        base="main",
        title="t",
        body="b",
        client=client,
    )
    assert url.endswith("/pull/1") and repo.created == [] and client.asked == ["acme/demo"]

    repo2 = _Repo([])
    url2 = await open_pr(
        "https://github.com/acme/demo",
        head="agent/x",
        base="main",
        title="t",
        body="b",
        client=_Client(repo2),
    )
    assert url2.endswith("/pull/2") and repo2.created[0]["head"] == "agent/x"


def test_pr_body_template() -> None:
    body = pr_body(
        goal="do x", diff_stat=" a.py | 1 +", test_summary="3 passed in 0.1s", run_id="r1"
    )
    assert "## Goal" in body and "a.py | 1 +" in body and "run `r1`" in body
