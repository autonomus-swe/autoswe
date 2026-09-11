"""Deterministic repository facts: detection rules, in the documented order."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo.profile import (
    collect,
    detect_install_command,
    detect_package_manager,
    detect_test_command,
)

pytestmark = pytest.mark.unit


def write(root: Path, rel: str, body: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_ci_workflow_wins_over_every_other_rule(tmp_path: Path) -> None:
    """A repo's own CI is the most reliable statement of how it is tested."""
    write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths=['tests']\n")
    write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs:\n  t:\n    steps:\n      - run: pytest -q --cov\n",
    )
    cmd, rule = detect_test_command(tmp_path)
    assert rule == "github-workflow" and "pytest -q --cov" in (cmd or "")


def test_pyproject_pytest_and_poetry(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths=['tests']\n")
    assert detect_test_command(tmp_path) == ("uv run --no-sync pytest -q", "pyproject-pytest")

    poetry = tmp_path / "poetry"
    write(poetry, "pyproject.toml", "[tool.poetry]\nname='x'\n")
    assert detect_test_command(poetry) == ("poetry run pytest -q", "pyproject-poetry")


def test_package_json_go_and_makefile(tmp_path: Path) -> None:
    node = tmp_path / "node"
    write(node, "package.json", '{"scripts": {"test": "jest"}}')
    assert detect_test_command(node) == ("npm test --silent", "package-json")

    go = tmp_path / "go"
    write(go, "go.mod", "module example.com/x\n")
    assert detect_test_command(go) == ("go test ./...", "go-mod")

    mk = tmp_path / "mk"
    write(mk, "Makefile", "test:\n\tpytest\n")
    assert detect_test_command(mk) == ("make test", "makefile")


def test_empty_repo_detects_nothing_rather_than_guessing(tmp_path: Path) -> None:
    assert detect_test_command(tmp_path) == (None, None)
    facts = collect(tmp_path)
    assert facts.test_command is None and facts.install_command is None
    assert facts.file_count == 0 and facts.languages == []


def test_package_manager_and_install_command(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    write(tmp_path, "uv.lock", "")
    assert detect_package_manager(tmp_path) == "uv"
    assert "uv sync" in (detect_install_command(tmp_path, "uv") or "")

    pip = tmp_path / "pip"
    write(pip, "requirements.txt", "pytest\n")
    assert detect_package_manager(pip) == "pip"
    assert "uv pip install -r requirements.txt" in (detect_install_command(pip, "pip") or "")

    assert detect_install_command(tmp_path, None) is None


def test_collect_gathers_the_whole_picture(tmp_path: Path) -> None:
    write(
        tmp_path,
        "pyproject.toml",
        "[project]\nrequires-python='>=3.12'\n\n[tool.pytest.ini_options]\ntestpaths=['tests']\n\n[tool.ruff]\nline-length=100\n",
    )
    write(tmp_path, "uv.lock", "")
    write(tmp_path, "README.md", "# demo\n\nA demo project.\n")
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/ops.py", "def add(a, b):\n    return a + b\n")
    write(tmp_path, "tests/test_ops.py", "def test(): ...\n")
    write(tmp_path, ".venv/lib/junk.py", "ignored\n")

    facts = collect(tmp_path)
    assert facts.languages == ["python"]
    assert facts.package_manager == "uv" and facts.test_command == "uv run --no-sync pytest -q"
    assert facts.lint_command == "uv run ruff check ."
    assert facts.python_version == ">=3.12"
    assert facts.file_count == 6  # .venv is not counted
    assert "pyproject.toml" in facts.manifests and "README.md" not in facts.manifests
    assert facts.readme_head.startswith("# demo")
    assert facts.detected_by == "pyproject-pytest"

    rendered = facts.render()
    assert "test command" in rendered and "uv run --no-sync pytest -q" in rendered
    assert "\n" in rendered and not rendered.startswith("{")  # text, never JSON


def test_malformed_manifests_do_not_raise(tmp_path: Path) -> None:
    """A repository in the wild may have broken config; detection must survive it."""
    write(tmp_path, "pyproject.toml", "this is not [valid toml")
    write(tmp_path, "package.json", "{not json")
    facts = collect(tmp_path)
    assert facts.test_command is None
