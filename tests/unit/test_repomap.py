"""Repo map v1: budget, ranking, and what it must never omit."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo.repomap import render_map

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
