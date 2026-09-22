"""The version the package reports has to be the version that was shipped.

It was not. `pyproject.toml` said `0.0.1` while the repository was tagged `v0.3.0`, so
`autoswe version` had been wrong since the first release — the kind of thing nobody
notices because nobody has a reason to look, until a bug report arrives quoting a version
that never existed.

Two checks, because they fail at different times. The changelog check runs everywhere and
catches a release prepared without a version bump. The tag check needs git history and is
skipped without it, but it is the one that catches a tag pushed against a stale version.
"""

from __future__ import annotations

import re
import subprocess
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def package_version() -> str:
    return installed_version("autoswe")


def test_the_package_reports_a_real_version() -> None:
    assert SEMVER.match(package_version()), package_version()
    assert package_version() != "0.0.1", "the placeholder from before the first release"


def test_the_changelog_documents_the_version_being_shipped() -> None:
    """A release whose notes are one version behind is a release nobody can read about."""
    headings = re.findall(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(), re.M)
    assert headings, "the changelog has no version headings at all"
    assert headings[0] == package_version(), (
        f"changelog's newest entry is {headings[0]} but the package is {package_version()}"
    )


def test_the_version_is_never_behind_the_latest_tag() -> None:
    """The check that would have caught the original drift, without failing a release.

    The bug this exists for was the version *behind* the tag: `pyproject.toml` said
    `0.0.1` while the repository was tagged `v0.3.0`. Equality caught that, and also
    caught the ordinary state of a release being prepared — the version bumped in the pull
    request that writes the changelog, the tag created after it merges. Between those two
    moments an equality check fails for a repository that is entirely correct, which turns
    a useful guard into one people learn to ignore.

    So: the version may be ahead of the newest tag, and may equal it, and may never be
    behind it. Ahead is "a release is prepared"; behind is the bug.

    Skipped where the tags are not fetched — CI checks out shallow — so it is a local and
    release-time guard rather than a gate. Better than nothing, and honest about which.
    """
    try:
        described = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f"git is not usable here: {e}")
    if described.returncode != 0:
        pytest.skip("no tags in this checkout (a shallow clone has none)")

    tag = described.stdout.strip().removeprefix("v")
    if not SEMVER.match(tag):
        pytest.skip(f"the newest tag is not a version: {tag!r}")

    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    assert parts(package_version()) >= parts(tag), (
        f"the package reports {package_version()} but the newest tag is v{tag}; "
        "a version behind its own tag is the drift this test exists for"
    )
