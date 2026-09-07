from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import make_ctx
from tools.editor import EditorTool

pytestmark = pytest.mark.unit


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".venv").mkdir()
    return tmp_path


async def test_view_directory_and_file(wt: Path) -> None:
    ed, ctx = EditorTool(), make_ctx(wt)
    listing = (await ed(ctx, command="view", path="/workspace")).content
    assert (
        "pkg/" in listing
        and "ops.py" in listing
        and ".git" not in listing
        and ".venv" not in listing
    )
    res = await ed(ctx, command="view", path="pkg/ops.py")
    assert res.content.splitlines()[0].strip().startswith("1\tdef add")
    assert "pkg/ops.py" in ctx.view_hashes
    ranged = await ed(ctx, command="view", path="pkg/ops.py", view_range=[2, -1])
    assert ranged.content.strip().startswith("2\t")


async def test_str_replace_requires_view_then_exact_once(wt: Path) -> None:
    ed, ctx = EditorTool(), make_ctx(wt)
    stale = await ed(
        ctx, command="str_replace", path="pkg/ops.py", old_str="a + b", new_str="a - b"
    )
    assert stale.is_error and "view" in stale.content
    await ed(ctx, command="view", path="pkg/ops.py")
    missing = await ed(ctx, command="str_replace", path="pkg/ops.py", old_str="nope", new_str="x")
    assert missing.is_error and "not found" in missing.content
    (wt / "pkg" / "ops.py").write_text("x = 1\nx = 1\n")
    await ed(ctx, command="view", path="pkg/ops.py")
    multi = await ed(ctx, command="str_replace", path="pkg/ops.py", old_str="x = 1", new_str="y")
    assert multi.is_error and "2 times" in multi.content
    okr = await ed(
        ctx, command="str_replace", path="pkg/ops.py", old_str="x = 1\nx = 1", new_str="z = 2"
    )
    assert not okr.is_error and (wt / "pkg" / "ops.py").read_text() == "z = 2\n"


async def test_str_replace_rejected_when_file_changed_since_view(wt: Path) -> None:
    ed, ctx = EditorTool(), make_ctx(wt)
    await ed(ctx, command="view", path="pkg/ops.py")
    (wt / "pkg" / "ops.py").write_text("changed = True\n")  # e.g. a bash command rewrote it
    res = await ed(ctx, command="str_replace", path="pkg/ops.py", old_str="changed", new_str="c")
    assert res.is_error and "changed since" in res.content


async def test_create_refuses_unviewed_overwrite(wt: Path) -> None:
    ed, ctx = EditorTool(), make_ctx(wt)
    res = await ed(ctx, command="create", path="pkg/ops.py", file_text="oops")
    assert res.is_error and "already exists" in res.content
    new = await ed(ctx, command="create", path="pkg/new/mod.py", file_text="print(1)\n")
    assert not new.is_error and (wt / "pkg" / "new" / "mod.py").read_text() == "print(1)\n"
    await ed(ctx, command="view", path="pkg/ops.py")
    over = await ed(ctx, command="create", path="pkg/ops.py", file_text="new\n")
    assert not over.is_error and (wt / "pkg" / "ops.py").read_text() == "new\n"


async def test_insert_after_line(wt: Path) -> None:
    ed, ctx = EditorTool(), make_ctx(wt)
    await ed(ctx, command="view", path="pkg/ops.py")
    res = await ed(ctx, command="insert", path="pkg/ops.py", insert_line=0, new_str="# header")
    assert not res.is_error
    assert (wt / "pkg" / "ops.py").read_text().startswith("# header\ndef add")
    bad = await ed(ctx, command="insert", path="pkg/ops.py", insert_line=99, new_str="x")
    assert bad.is_error


async def test_path_escape_is_a_policy_violation(wt: Path) -> None:
    from core.errors import PolicyViolation

    with pytest.raises(PolicyViolation):
        await EditorTool()(make_ctx(wt), command="view", path="../../etc/passwd")
