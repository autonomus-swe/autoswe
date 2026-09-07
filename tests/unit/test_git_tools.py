from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import RepoError
from repo.gitcmd import git
from tests.fakes import make_ctx
from tools.git import GitCommitTool, GitDiffTool, GitStatusTool
from tools.registry import READ_ONLY_ROLES, REGISTRY, tools_for

pytestmark = pytest.mark.unit


@pytest.fixture
async def wt(tmp_path: Path) -> Path:
    await git("init", "-q", "-b", "agent/test", cwd=tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    await git("add", "-A", cwd=tmp_path)
    await git("commit", "-q", "-m", "init", cwd=tmp_path)
    return tmp_path


async def test_status_diff_commit_roundtrip(wt: Path) -> None:
    base = (await git("rev-parse", "HEAD", cwd=wt)).strip()
    ctx = make_ctx(wt, base_sha=base)
    assert "(clean" in (await GitStatusTool()(ctx)).content
    (wt / "a.py").write_text("x = 2\n")
    assert "a.py" in (await GitStatusTool()(ctx)).content
    assert "-x = 1" in (await GitDiffTool()(ctx)).content
    bad = await GitCommitTool()(ctx, message="changed stuff")
    assert bad.is_error and "feat|fix" in bad.content
    res = await GitCommitTool()(ctx, message="fix(a): set x to 2")
    assert not res.is_error and res.content.startswith("committed ")
    again = await GitCommitTool()(ctx, message="fix: nothing")
    assert again.is_error and "nothing to commit" in again.content


async def test_commit_refuses_wrong_branch(wt: Path) -> None:
    ctx = make_ctx(wt, work_branch="agent/other")
    (wt / "b.py").write_text("y = 1\n")
    with pytest.raises(RepoError, match="refusing to commit"):
        await GitCommitTool()(ctx, message="feat: b")


def test_registry_roles() -> None:
    names = [t.name for t in tools_for("coder")]
    assert names[:2] == ["bash", "str_replace_based_edit_tool"] and "git_commit" in names
    for role in READ_ONLY_ROLES:
        assert all(not t.mutating for t in tools_for(role))
    for tool in REGISTRY.values():
        assert (
            tool.input_schema["type"] == "object"
            and tool.input_schema["additionalProperties"] is False
        )
