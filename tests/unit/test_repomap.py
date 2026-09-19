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
    I ran out of room" and "this does not exist" are different facts.

    The budget here is large enough to hold the tail. It used to be 12 tokens, and passed
    only because the tail was appended outside the budget — once the budget was actually
    enforced, 12 tokens could not hold "other files (…)" and the assertion failed. The
    floor case is its own test below, because it is a different claim.
    """
    files, symbols = auth_service(tmp_path)
    ranks = graph.build(tmp_path, files, symbols).rank

    out = repomap.render_symbol_map(tmp_path, symbols, ranks, goal="token", token_budget=40)

    assert "other files (" in out
    assert len(out) <= int(40 * repomap.CHARS_PER_TOKEN)


def test_a_budget_too_small_for_anything_still_names_one_file(tmp_path: Path) -> None:
    """The floor. A map cannot say less than which file it would have shown, so below that
    the budget loses — but it loses to one path, not to a whole file's worth of symbols and
    a list of four hundred more."""
    files, symbols = auth_service(tmp_path)
    ranks = graph.build(tmp_path, files, symbols).rank

    out = repomap.render_symbol_map(tmp_path, symbols, ranks, goal="token", token_budget=1)

    assert out.count("\n") == 0, f"more than one line at a one-token budget: {out!r}"
    assert out in files


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


@pytest.mark.parametrize("budget", [200, 500, 1_500, 3_500])
def test_the_map_never_exceeds_the_budget_it_was_given(tmp_path: Path, budget: int) -> None:
    """`token_budget` has to bound the whole map, not the ranked half of it.

    It did not. The "other files" tail was appended after the loop that watched the budget,
    so on a repository with more files than fit the map ran over by however long that list
    was — measured on sympy, a 3 500-token budget rendering a 5 300-token map. Parametrised
    because the first fix worked at one budget and not at the one where the blocks filled
    it exactly and the tail was pure overshoot.
    """
    symbols = []
    for i in range(400):
        path = f"pkg/mod{i}.py"
        (tmp_path / "pkg").mkdir(exist_ok=True)
        (tmp_path / path).write_text(f"def function_number_{i}(argument):\n    return {i}\n")
        symbols.append(
            Symbol(
                path=path,
                kind="function",
                name=f"function_number_{i}",
                signature=f"def function_number_{i}(argument):",
                start_line=1,
                end_line=2,
                refs=[],
            )
        )
    ranks = {s.path: 1.0 / (i + 1) for i, s in enumerate(symbols)}

    out = repomap.render_symbol_map(tmp_path, symbols, ranks, goal="function", token_budget=budget)

    assert len(out) <= int(budget * repomap.CHARS_PER_TOKEN), (
        f"{len(out)} chars against a {int(budget * repomap.CHARS_PER_TOKEN)}-char budget"
    )


def test_a_truncated_map_says_how_many_files_it_did_not_show(tmp_path: Path) -> None:
    """ "this file exists and I ran out of room" and "this file does not exist" are
    different facts. The header carries the real count even when the list is empty."""
    symbols = []
    for i in range(300):
        path = f"m{i}.py"
        (tmp_path / path).write_text("def f():\n    return 1\n")
        symbols.append(
            Symbol(
                path=path,
                kind="function",
                name="f",
                signature="def f():",
                start_line=1,
                end_line=2,
                refs=[],
            )
        )

    out = repomap.render_symbol_map(
        tmp_path, symbols, {s.path: 1.0 for s in symbols}, token_budget=200
    )

    assert "other files (" in out
    assert "more not shown" in out or out.count("\n  m") > 0
