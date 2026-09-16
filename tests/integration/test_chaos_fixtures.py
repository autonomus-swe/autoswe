"""The chaos fixtures are a claim about how each scenario fails. This checks the claim.

A fixture nobody verifies is a fixture that drifts, and an e2e run against a scenario that
no longer fails the way its README says wastes a real model's turns proving nothing. These
run the fixture's own suite on every branch — no sandbox, no model, a couple of seconds.

The classifications are the parser's, on the real report, so a change to either the
scenarios or the classifier shows up here rather than in a paid run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repo.gitcmd import git
from tests.e2e.chaos import ALL, branches, materialise
from tools.test_report import parse_json_report

pytestmark = pytest.mark.integration

REPORT = ".autoswe/report.json"


def _has_json_report() -> bool:
    return importlib_found("pytest_jsonreport")


def importlib_found(module: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


needs_json_report = pytest.mark.skipif(
    not _has_json_report(),
    reason="pytest-json-report is not installed in this environment (the sandbox has it)",
)


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("chaos") / "origin"
    asyncio.run(materialise(root))
    return root


def run_suite(repo: Path) -> dict[str, object]:
    """Run the fixture's own tests on whatever branch is checked out."""
    report = repo / REPORT
    report.parent.mkdir(exist_ok=True)
    report.unlink(missing_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--json-report",
            f"--json-report-file={REPORT}",
        ],
        cwd=repo,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin", "HOME": str(repo)},
        capture_output=True,
        check=False,
    )
    assert report.is_file(), "the suite produced no report at all"
    return dict(json.loads(report.read_text()))


async def checkout(repo: Path, branch: str) -> None:
    await git("checkout", "-q", branch, cwd=repo)


def test_every_scenario_directory_is_a_branch(repo: Path) -> None:
    """The scenario name *is* the branch name, because base_branch is how one is chosen."""
    listed = asyncio.run(git("branch", "--format=%(refname:short)", cwd=repo)).split()
    assert sorted(b for b in listed if b != "main") == branches()
    assert sorted(s.branch for s in ALL) == branches(), "chaos.py and the directories agree"


def test_every_goal_names_the_test_file_it_is_about(repo: Path) -> None:
    """A task whose selector is known is the only case the baseline filter is safe in."""
    for scenario in ALL:
        assert "tests/" in scenario.goal, scenario.branch
        assert "not change the tests" in scenario.goal.lower(), scenario.branch


@needs_json_report
def test_main_is_green(repo: Path) -> None:
    """Every scenario is a commit on top of this, so a red main would muddy all of them."""
    asyncio.run(checkout(repo, "main"))
    report = parse_json_report(run_suite(repo), "pytest -q", worktree=repo)
    assert report.passed and report.total == 3, report.failures


@needs_json_report
def test_the_off_by_one_arrives_as_an_assertion_with_both_values(repo: Path) -> None:
    """An assertion failure has no frame in the implementation, and that is not a gap.

    Nothing raised inside `paginate` — it returned the wrong answer — so the only frame is
    the assert. What makes the failure diagnosable is the *comparison*: the message has to
    carry both sides, which is why `_first_line` prefers the line naming the exception over
    pytest's trailing "Use -v to get more diff".
    """
    asyncio.run(checkout(repo, "a-off-by-one"))
    report = parse_json_report(run_suite(repo), "pytest -q", worktree=repo)

    assert not report.passed and report.failed == 2
    assert {f.kind for f in report.failures} == {"assertion"}
    dropped = next(f for f in report.failures if "partial_page" in f.test_id)
    assert "[[1, 2], [3, 4]] == [[1, 2], [3, 4], [5]]" in dropped.message
    assert [f.file for f in dropped.frames] == ["tests/test_pages.py"], "no duplicate frames"
    assert dropped.frames[0].code.startswith("assert paginate("), "read from disk"


@needs_json_report
def test_the_missing_import_is_classified_as_import(repo: Path) -> None:
    """A forgotten import arrives as a bare NameError during collection; `exception`
    would tell the Debugger nothing."""
    asyncio.run(checkout(repo, "b-missing-import"))
    report = parse_json_report(run_suite(repo), "pytest -q", worktree=repo)

    assert not report.passed and report.errors == 1
    (failure,) = report.failures
    assert failure.kind == "import"
    assert failure.frames, "a collection failure still names the file and the line"
    assert failure.frames[-1].file == "chaos/stamp.py"


@needs_json_report
def test_the_impossible_test_cannot_be_satisfied(repo: Path) -> None:
    """Both assertions are about the same call, so no implementation passes both."""
    asyncio.run(checkout(repo, "c-impossible"))
    report = parse_json_report(run_suite(repo), "pytest -q", worktree=repo)

    assert not report.passed
    ids = {f.test_id for f in report.failures}
    assert "tests/test_impossible.py::test_paginate_is_both_inclusive_and_exclusive" in ids
    source = (repo / "tests" / "test_impossible.py").read_text()
    assert source.count("assert len(paginate([1, 2, 3, 4, 5], 2))") == 2
    assert "== 3" in source and "== 2" in source


@needs_json_report
def test_the_baseline_scenario_fails_two_ways_at_once(repo: Path) -> None:
    """One failure the run is asked about, one it merely inherited."""
    asyncio.run(checkout(repo, "e-baseline"))
    report = parse_json_report(run_suite(repo), "pytest -q", worktree=repo)

    ids = {f.test_id for f in report.failures}
    assert any("test_legacy" in i for i in ids), "the inherited failure"
    assert any("test_pages" in i for i in ids), "the one the goal is about"
    assert len({i.split("::")[0] for i in ids}) == 2, "in two different files, so they separate"


def test_the_network_scenario_needs_a_sandbox_to_fail(repo: Path) -> None:
    """It passes on a networked host, which is exactly why it is a scenario.

    The failure it is for — `environment` rather than a bug — only happens where the agent
    actually works, inside a sandbox with the network disconnected. Asserting it fails here
    would mean asserting that this machine is offline.
    """
    asyncio.run(checkout(repo, "d-network"))
    source = (repo / "chaos" / "fetch.py").read_text()
    assert (
        "urlopen" in source
        and "https://example.com" in (repo / "tests" / "test_fetch.py").read_text()
    )


def test_the_fixture_is_a_copy_and_nothing_writes_back(repo: Path) -> None:
    """Materialising must not touch the checked-in fixture: a run edits the clone."""
    from tests.e2e.chaos import BASE

    asyncio.run(checkout(repo, "main"))
    assert (BASE / "chaos" / "pages.py").read_text() == (repo / "chaos" / "pages.py").read_text()
    assert not (BASE / ".git").exists(), "the fixture source is files, not a repository"
    shutil.rmtree(repo / ".autoswe", ignore_errors=True)
