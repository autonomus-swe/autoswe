"""Deterministic repository facts: what we can know without asking a model.

The Analyzer confirms or corrects these; detecting them here keeps a whole class of
questions away from the model and means `SETUP` no longer hard-codes an install command.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from contracts import RepoFacts

SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
)
MANIFESTS = (
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
    "Makefile",
)
EXT_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".php": "php",
    ".sh": "shell",
    ".c": "c",
    ".cpp": "c++",
    ".cs": "c#",
}
LANGUAGE_SHARE = 0.05
README_LINES = 60
CI_TEST_MARKERS = ("pytest", "npm test", "pnpm test", "yarn test", "go test", "cargo test")


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and not any(p in SKIP_DIRS for p in path.relative_to(root).parts):
            out.append(path)
    return out


def _languages(files: list[Path]) -> list[str]:
    counts: dict[str, int] = {}
    for f in files:
        lang = EXT_LANGUAGE.get(f.suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    total = sum(counts.values())
    if not total:
        return []
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return [lang for lang, n in ranked if n / total >= LANGUAGE_SHARE]


def _ci_test_lines(root: Path) -> list[str]:
    lines: list[str] = []
    for wf in (
        sorted((root / ".github" / "workflows").glob("*.y*ml"))
        if (root / ".github" / "workflows").is_dir()
        else []
    ):
        try:
            for raw in wf.read_text(errors="replace").splitlines():
                stripped = raw.strip()
                if stripped.startswith(("run:", "-")) and any(
                    m in stripped for m in CI_TEST_MARKERS
                ):
                    lines.append(stripped)
        except OSError:
            continue
    return lines[:10]


def _pyproject(root: Path) -> dict[str, object]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _package_json(root: Path) -> dict[str, object]:
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(errors="replace"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _makefile_targets(root: Path) -> set[str]:
    path = root / "Makefile"
    if not path.is_file():
        return set()
    return set(re.findall(r"^([A-Za-z0-9_.-]+):", path.read_text(errors="replace"), re.M))


def detect_test_command(root: Path) -> tuple[str | None, str | None]:
    """``(command, which rule fired)``. First hit wins, in the spec's order."""
    for line in _ci_test_lines(root):
        for marker in CI_TEST_MARKERS:
            if marker in line:
                cleaned = re.sub(r"^-?\s*(run:)?\s*", "", line).strip()
                return cleaned, "github-workflow"

    pyproject = _pyproject(root)
    tool = pyproject.get("tool", {}) if isinstance(pyproject.get("tool"), dict) else {}
    if isinstance(tool, dict):
        if "pytest" in tool:
            return "uv run --no-sync pytest -q", "pyproject-pytest"
        if "poetry" in tool:
            return "poetry run pytest -q", "pyproject-poetry"

    scripts = _package_json(root).get("scripts")
    if isinstance(scripts, dict) and scripts.get("test"):
        return "npm test --silent", "package-json"

    if (root / "go.mod").is_file():
        return "go test ./...", "go-mod"

    if "test" in _makefile_targets(root):
        return "make test", "makefile"

    return None, None


def detect_package_manager(root: Path) -> str | None:
    if (root / "pyproject.toml").is_file():
        if (root / "uv.lock").is_file():
            return "uv"
        tool = _pyproject(root).get("tool")
        if isinstance(tool, dict) and "poetry" in tool:
            return "poetry"
        return "uv"
    if (root / "requirements.txt").is_file():
        return "pip"
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "package.json").is_file():
        return "npm"
    if (root / "go.mod").is_file():
        return "go"
    return None


def detect_install_command(root: Path, package_manager: str | None) -> str | None:
    return {
        "uv": "uv sync --all-extras || uv sync",
        "poetry": "poetry install",
        "pip": "uv venv && uv pip install -r requirements.txt",
        "npm": "npm ci",
        "pnpm": "pnpm install --frozen-lockfile",
        "yarn": "yarn install --frozen-lockfile",
        "go": "go mod download",
    }.get(package_manager or "")


def detect_lint_command(root: Path) -> str | None:
    tool = _pyproject(root).get("tool")
    if isinstance(tool, dict) and "ruff" in tool:
        return "uv run ruff check ."
    scripts = _package_json(root).get("scripts")
    if isinstance(scripts, dict) and scripts.get("lint"):
        return "npm run lint"
    if "lint" in _makefile_targets(root):
        return "make lint"
    return None


def detect_python_version(root: Path) -> str | None:
    pinned = root / ".python-version"
    if pinned.is_file():
        return pinned.read_text(errors="replace").strip() or None
    project = _pyproject(root).get("project")
    if isinstance(project, dict):
        requires = project.get("requires-python")
        if isinstance(requires, str):
            return requires
    return None


def collect(root: Path) -> RepoFacts:
    """Everything deterministic about the repository at ``root``."""
    files = _walk(root)
    test_command, detected_by = detect_test_command(root)
    package_manager = detect_package_manager(root)
    readme = next(
        (root / n for n in ("README.md", "README.rst", "README") if (root / n).is_file()), None
    )
    return RepoFacts(
        languages=_languages(files),
        package_manager=package_manager,
        install_command=detect_install_command(root, package_manager),
        test_command=test_command,
        lint_command=detect_lint_command(root),
        python_version=detect_python_version(root),
        file_count=len(files),
        top_level=sorted(
            p.name + ("/" if p.is_dir() else "") for p in root.iterdir() if p.name not in SKIP_DIRS
        ),
        manifests=[m for m in MANIFESTS if (root / m).is_file()],
        ci_test_lines=_ci_test_lines(root),
        readme_head="\n".join(readme.read_text(errors="replace").splitlines()[:README_LINES])
        if readme
        else "",
        detected_by=detected_by,
    )
