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


def test_the_version_matches_the_latest_tag() -> None:
    """The check that would have caught the original drift.

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
    assert tag == package_version(), (
        f"the newest tag is v{tag} but the package reports {package_version()}"
    )
