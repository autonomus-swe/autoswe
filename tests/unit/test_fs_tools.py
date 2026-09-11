"""read_file and search_code: ranges, caps, path confinement, ripgrep parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import PolicyViolation
from tests.fakes import make_ctx
from tools.editor import EditorTool
from tools.fs import DEFAULT_LINES, ReadFileTool, looks_binary
from tools.search import SearchCodeTool, parse_rg_json

pytestmark = pytest.mark.unit


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "pkg" / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 1001)))
    (tmp_path / "pkg" / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ops.py").write_text(
        "from pkg.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "secret.py").write_text("def add(a, b): ...\n")
    return tmp_path


async def test_read_file_numbers_lines_and_caps_long_files(wt: Path) -> None:
    tool, ctx = ReadFileTool(), make_ctx(wt)
    res = await tool(ctx, path="pkg/ops.py")
    assert not res.is_error and res.content.splitlines()[0].strip().startswith("1\tdef add")

    long = await tool(ctx, path="pkg/big.py")
    lines = [ln for ln in long.content.splitlines() if "\t" in ln]
    assert len(lines) == DEFAULT_LINES
    assert f"showing 1 to {DEFAULT_LINES} of 1000" in long.content
    assert f"start_line={DEFAULT_LINES + 1}" in long.content

    rest = await tool(ctx, path="pkg/big.py", start_line=401)
    assert rest.content.splitlines()[0].strip().startswith("401\tline 401")


async def test_read_file_records_the_hash_so_a_later_edit_is_not_stale(wt: Path) -> None:
    """Reading counts as viewing: the editor's staleness check must accept it."""
    ctx = make_ctx(wt)
    await ReadFileTool()(ctx, path="pkg/ops.py")
    assert "pkg/ops.py" in ctx.view_hashes
    edit = await EditorTool()(
        ctx, command="str_replace", path="pkg/ops.py", old_str="a + b", new_str="a - b"
    )
    assert not edit.is_error
    assert (wt / "pkg" / "ops.py").read_text() == "def add(a, b):\n    return a - b\n"


async def test_read_file_rejects_directories_binaries_and_escapes(wt: Path) -> None:
    tool, ctx = ReadFileTool(), make_ctx(wt)
    assert (await tool(ctx, path="pkg")).is_error
    assert (await tool(ctx, path="pkg/blob.bin")).is_error
    assert (await tool(ctx, path="pkg/nope.py")).is_error
    assert (await tool(ctx, path="pkg/ops.py", start_line=5, end_line=2)).is_error
    with pytest.raises(PolicyViolation):
        await tool(ctx, path="../../etc/passwd")


def test_looks_binary() -> None:
    assert looks_binary(b"\x00abc") and not looks_binary(b"plain text\n")


async def test_search_code_finds_matches_and_skips_vendored_directories(wt: Path) -> None:
    tool, ctx = SearchCodeTool(), make_ctx(wt)
    res = await tool(ctx, pattern="def add")
    assert not res.is_error
    assert "pkg/ops.py" in res.content
    assert ".venv" not in res.content  # never search vendored trees

    globbed = await tool(ctx, pattern="add", glob="tests/*.py")
    assert "tests/test_ops.py" in globbed.content and "pkg/ops.py" not in globbed.content

    none = await tool(ctx, pattern="zzz_no_such_symbol")
    assert not none.is_error and "no matches" in none.content

    assert (await tool(ctx, pattern="")).is_error


async def test_search_code_literal_mode(wt: Path) -> None:
    """A regex metacharacter is a literal when fixed_string is set."""
    (wt / "pkg" / "re.py").write_text("value = a + b\n")
    tool, ctx = SearchCodeTool(), make_ctx(wt)
    assert "pkg/re.py" in (await tool(ctx, pattern="a + b", fixed_string=True)).content


def test_parse_rg_json_renders_and_counts_omitted() -> None:
    import json

    def match(path: str, line: int, text: str) -> str:
        return json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": path},
                    "line_number": line,
                    "lines": {"text": text + "\n"},
                },
            }
        )

    stdout = "\n".join(
        [
            json.dumps({"type": "begin", "data": {}}),
            match("a.py", 3, "def add(a, b):"),
            match("b.py", 7, "    add(1, 2)"),
            match("c.py", 9, "    add(3, 4)"),
            "not json",
            json.dumps({"type": "end", "data": {}}),
        ]
    )
    hits, omitted = parse_rg_json(stdout, max_results=2)
    assert hits == ["a.py:3: def add(a, b):", "b.py:7:     add(1, 2)"]
    assert omitted == 1
    assert parse_rg_json("", 50) == ([], 0)


async def test_search_works_without_ripgrep_installed(
    wt: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ripgrep is a fast path, not a requirement: the fallback must give the same answers."""
    monkeypatch.setattr("tools.search.ripgrep_path", lambda: None)
    tool, ctx = SearchCodeTool(), make_ctx(wt)

    res = await tool(ctx, pattern="def add")
    assert "pkg/ops.py" in res.content and ".venv" not in res.content

    globbed = await tool(ctx, pattern="add", glob="tests/*.py")
    assert "tests/test_ops.py" in globbed.content and "pkg/ops.py" not in globbed.content

    literal = await tool(ctx, pattern="a + b", fixed_string=True)
    assert "pkg/ops.py" in literal.content

    assert "no matches" in (await tool(ctx, pattern="zzz_no_such_symbol")).content


def test_python_search_skips_binaries_and_counts_omitted(tmp_path: Path) -> None:
    from tools.search import python_search

    (tmp_path / "a.py").write_text("needle\nneedle\nneedle\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00needle")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "v.py").write_text("needle\n")

    hits, omitted = python_search(tmp_path, "needle", glob=None, max_results=2, fixed_string=True)
    assert hits == ["a.py:1: needle", "a.py:2: needle"]
    assert omitted == 1  # the third match in a.py; binary and .venv never counted
