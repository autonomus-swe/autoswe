"""Repo map v1: budget, ranking, and what it must never omit."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo import graph, repomap
from repo.repomap import render_map
from repo.symbols import Symbol

pytestmark = pytest.mark.unit


def build(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "README.md").write_text("# x\n")
    for pkg in ("core", "gateway"):
        (root / pkg).mkdir()
        (root / pkg / "__init__.py").write_text("")
        for i in range(4):
            (root / pkg / f"mod{i}.py").write_text("x = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test(): ...\n")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# guide\n")
    (root / ".venv").mkdir()
    (root / ".venv" / "junk.py").write_text("ignored\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def test_manifests_first_then_source_then_tests_then_docs(tmp_path: Path) -> None:
    build(tmp_path)
    out = render_map(tmp_path, max_lines=100)
    lines = out.splitlines()
    # top-level manifests lead, as a group and in name order
    assert [ln.split()[0] for ln in lines[:2]] == ["README.md", "pyproject.toml"], out
    order = [
        next(i for i, ln in enumerate(lines) if ln.strip().startswith(n))
        for n in ("core/", "tests/", "docs/")
    ]
    assert order == sorted(order), out


def test_vendored_trees_and_binaries_are_never_shown(tmp_path: Path) -> None:
    build(tmp_path)
    out = render_map(tmp_path, max_lines=100)
    assert ".venv" not in out and "junk.py" not in out
    assert "logo.png" not in out


def test_budget_is_respected_and_the_remainder_is_counted(tmp_path: Path) -> None:
    build(tmp_path)
    out = render_map(tmp_path, max_lines=6)
    body = [ln for ln in out.splitlines() if not ln.startswith("…")]
    assert len(body) <= 6
    assert out.splitlines()[-1].startswith("… (+") and "more files" in out.splitlines()[-1]
    # the budget must never cost you the manifests
    assert "pyproject.toml" in out


def test_empty_and_missing_worktrees(tmp_path: Path) -> None:
    assert render_map(tmp_path) == "(empty repository)"
    assert render_map(tmp_path / "nope") == "(no worktree)"


def test_flat_packages_rank_as_source_even_without_a_src_directory(tmp_path: Path) -> None:
    """Naming varies; ranking follows what a directory contains."""
    build(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "workflows").mkdir()
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    out = render_map(tmp_path, max_lines=100)
    lines = out.splitlines()
    core_at = next(i for i, ln in enumerate(lines) if ln.strip().startswith("core/"))
    dot_at = next(i for i, ln in enumerate(lines) if ln.strip().startswith(".github/"))
    assert core_at < dot_at, out


# ---- v2: ranked by centrality and lexical relevance ---------------------------------------


def auth_service(root: Path) -> tuple[list[str], list[Symbol]]:
    """A small service shaped like the one the phase document's criterion describes.

    `app/auth/jwt.py` is both *named by the goal* and *depended on* — which is the case the
    0.6/0.4 weighting is built for. A file that only matched lexically would not beat a
    central one, and that is the weighting working rather than failing: a goal mentioning a
    common word should not evict the module everything imports.
    """
    files = {
        "pyproject.toml": "[project]\nname='svc'\n",
        "app/main.py": "from app.auth.jwt import decode_token\nfrom app.db import session\n",
        "app/auth/jwt.py": "import time\n\n\ndef decode_token(t):\n    return {}\n",
        "app/auth/routes.py": "from app.auth.jwt import decode_token\n",
        "app/db.py": "def session(): pass\n",
        "tests/test_jwt.py": "from app.auth.jwt import decode_token\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    sources = [f for f in files if f.endswith(".py")]
    symbols = [
        Symbol(
            path="app/auth/jwt.py",
            kind="function",
            name="decode_token",
            signature="def decode_token(t):",
            start_line=4,
            end_line=5,
            refs=["time"],
        ),
        Symbol(
            path="app/db.py",
            kind="function",
            name="session",
            signature="def session():",
            start_line=1,
            end_line=1,
            refs=[],
        ),
        Symbol(
            path="app/main.py",
            kind="function",
            name="main",
            signature="def main():",
            start_line=1,
            end_line=1,
            refs=["decode_token", "session"],
        ),
        Symbol(
            path="app/auth/routes.py",
            kind="function",
            name="me",
            signature="def me():",
            start_line=1,
            end_line=1,
            refs=["decode_token"],
        ),
        Symbol(
            path="tests/test_jwt.py",
            kind="function",
            name="test_decode",
            signature="def test_decode():",
            start_line=1,
            end_line=1,
            refs=["decode_token"],
        ),
    ]
    return sources, symbols


def test_a_goal_naming_a_function_ranks_that_functions_file_first(tmp_path: Path) -> None:
    """The phase document's criterion, on the shape it describes."""
    files, symbols = auth_service(tmp_path)
    ranks = graph.build(tmp_path, files, symbols).rank

    order = repomap.rank_files(files, symbols, ranks, goal="fix the token expiry check in jwt")

    assert order[0][0] == "app/auth/jwt.py", order[:3]


def test_a_file_the_plan_named_is_pinned_even_when_nothing_points_at_it(
    tmp_path: Path,
) -> None:
    """A reader who does not see the file the plan named will wonder whether the map knew
    about it. Pinned rather than boosted, so it cannot be edged out."""
    files, symbols = auth_service(tmp_path)
    ranks = graph.build(tmp_path, files, symbols).rank

    order = repomap.rank_files(files, symbols, ranks, goal="unrelated", pinned=["app/db.py"])

    assert order[0][0] == "app/db.py"


def test_a_test_file_is_demoted_unless_the_goal_is_about_tests(tmp_path: Path) -> None:
    files, symbols = auth_service(tmp_path)
    ranks = graph.build(tmp_path, files, symbols).rank

    neutral = dict(repomap.rank_files(files, symbols, ranks, goal="decode a token"))
    about_tests = dict(repomap.rank_files(files, symbols, ranks, goal="fix the failing tests"))

    assert about_tests["tests/test_jwt.py"] > neutral["tests/test_jwt.py"]


def test_the_rendered_map_shows_definitions_rather_than_file_sizes(tmp_path: Path) -> None:
    files, symbols = auth_service(tmp_path)
    ranks = graph.build(tmp_path, files, symbols).rank

    out = repomap.render_symbol_map(tmp_path, symbols, ranks, goal="decode a token")

    assert "app/auth/jwt.py" in out
    assert "def decode_token(t):" in out
    assert "[L4-5]" in out, "with the range, so a reader can go straight there"


def test_the_token_budget_is_honoured_and_what_fell_out_is_named(tmp_path: Path) -> None:
    """A truncated map that silently drops files reads as a complete one. "This exists and
    I ran out of room" and "this does not exist" are different facts."""
    files, symbols = auth_service(tmp_path)
    ranks = graph.build(tmp_path, files, symbols).rank

    out = repomap.render_symbol_map(tmp_path, symbols, ranks, goal="token", token_budget=12)

    assert "other files (" in out
    assert len(out) <= 12 * repomap.CHARS_PER_TOKEN + 400, "the budget plus the omitted list"


def test_tokenize_splits_the_two_house_styles(tmp_path: Path) -> None:
    """`parse_json_report` and `parseJSONReport` both have to match "parse report", or the
    lexical half only works for code written one way."""
    assert "parse" in repomap.tokenize("parse_json_report")
    assert "parse" in repomap.tokenize("parseJsonReport")
    assert "report" in repomap.tokenize("parseJsonReport")


def test_the_v1_tree_is_still_there_for_a_repository_with_no_index(tmp_path: Path) -> None:
    """An unsupported language, or a tree nothing parsed. A worse map beats no map."""
    (tmp_path / "main.rb").write_text("def hi; end\n")

    assert repomap.render_symbol_map(tmp_path, [], {}) == ""
    assert "main.rb" in render_map(tmp_path)
