"""Loading and validating eval task files.

Most of these are rejections, and that is the point of validating here at all. A suite is
an hour of real runs and real money; a goal the control plane will refuse with a 422 on
task twenty-nine should stop the suite before task one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import ConfigError
from evals import suite

pytestmark = pytest.mark.unit

VALID = """
id: demo
repo: "${FIXTURE_REPO}"
goal: "Implement subtract(a, b) so the tests pass."
verify:
  command: "uv run pytest -q"
  expect_exit: 0
tags: [feature]
budget_usd: 3.0
"""
ENV = {"FIXTURE_REPO": "https://github.com/acme/fixture"}


def write(tmp_path: Path, name: str, body: str) -> Path:
    root = tmp_path / "private"
    root.mkdir(exist_ok=True)
    (root / f"{name}.yaml").write_text(body)
    return tmp_path


def test_a_valid_task_loads(tmp_path: Path) -> None:
    s = suite.load("private", directory=write(tmp_path, "demo", VALID), environ=ENV)
    [task] = s.tasks
    assert task.id == "demo"
    assert task.repo == "https://github.com/acme/fixture"
    assert task.verify is not None and task.verify.command == "uv run pytest -q"
    assert task.tags == ("feature",) and task.budget_usd == 3.0
    assert task.suite == "private"


def test_a_missing_suite_is_an_error_not_an_empty_one(tmp_path: Path) -> None:
    """ "You named a suite that does not exist" and "the suite resolved none of nothing"
    are different outcomes, and 0/0 reported as a result tells you nothing."""
    with pytest.raises(ConfigError, match="no such suite"):
        suite.load("private", directory=tmp_path, environ=ENV)


def test_an_empty_suite_directory_is_an_error_too(tmp_path: Path) -> None:
    (tmp_path / "private").mkdir()
    with pytest.raises(ConfigError, match="no tasks"):
        suite.load("private", directory=tmp_path, environ=ENV)


def test_an_unset_repository_variable_stops_the_suite(tmp_path: Path) -> None:
    """Otherwise every task fails to clone and the suite reports 0 % for a reason no row
    records."""
    with pytest.raises(ConfigError, match="FIXTURE_REPO"):
        suite.load("private", directory=write(tmp_path, "demo", VALID), environ={})


def test_a_goal_the_control_plane_would_refuse_is_refused_here(tmp_path: Path) -> None:
    body = VALID.replace('goal: "Implement subtract(a, b) so the tests pass."', 'goal: "fix"')
    with pytest.raises(ConfigError, match="characters"):
        suite.load("private", directory=write(tmp_path, "demo", body), environ=ENV)


@pytest.mark.parametrize(
    "repo",
    [
        "http://github.com/acme/fixture",
        "https://gitlab.com/acme/fixture",
        "https://github.com/acme",
    ],
)
def test_a_repository_the_control_plane_would_refuse_is_refused_here(
    tmp_path: Path, repo: str
) -> None:
    body = VALID.replace('repo: "${FIXTURE_REPO}"', f'repo: "{repo}"')
    with pytest.raises(ConfigError, match=r"github\.com/owner/name"):
        suite.load("private", directory=write(tmp_path, "demo", body), environ=ENV)


def test_a_verify_block_without_a_command_is_refused(tmp_path: Path) -> None:
    body = VALID.replace('  command: "uv run pytest -q"\n', "")
    with pytest.raises(ConfigError, match="needs a `command`"):
        suite.load("private", directory=write(tmp_path, "demo", body), environ=ENV)


def test_a_task_with_no_verify_block_is_allowed(tmp_path: Path) -> None:
    """Allowed, and reported `resolved: null` by the runner. It is the runner's job not to
    count it as a success; refusing it here would rule out tasks a human scores."""
    body = VALID.split("verify:")[0] + "tags: [feature]\n"
    s = suite.load("private", directory=write(tmp_path, "demo", body), environ=ENV)
    assert s.tasks[0].verify is None


def test_duplicate_task_ids_are_refused(tmp_path: Path) -> None:
    """Two rows with the same id in a results file cannot be told apart afterwards."""
    root = write(tmp_path, "one", VALID)
    (root / "private" / "two.yaml").write_text(VALID)
    with pytest.raises(ConfigError, match="duplicate task ids"):
        suite.load("private", directory=root, environ=ENV)


def test_an_id_defaults_to_the_filename(tmp_path: Path) -> None:
    body = VALID.replace("id: demo\n", "")
    s = suite.load("private", directory=write(tmp_path, "from-filename", body), environ=ENV)
    assert s.tasks[0].id == "from-filename"


def test_tags_select_a_subset_and_no_tags_selects_everything(tmp_path: Path) -> None:
    root = write(tmp_path, "feature-task", VALID)
    (root / "private" / "bug-task.yaml").write_text(
        VALID.replace("id: demo", "id: bug").replace("tags: [feature]", "tags: [bugfix]")
    )
    s = suite.load("private", directory=root, environ=ENV)
    assert {t.id for t in s.tagged(()).tasks} == {"demo", "bug"}
    assert {t.id for t in s.tagged(("bugfix",)).tasks} == {"bug"}
    assert s.tagged(("nonesuch",)).tasks == ()


def test_malformed_yaml_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        suite.load("private", directory=write(tmp_path, "bad", "id: [unclosed\n"), environ=ENV)


def test_the_shipped_private_suite_loads() -> None:
    """The tasks in this repository are documentation people copy. One the loader rejects
    is worse than none, and this is what rots the first time a rule is added."""
    s = suite.load("private", environ={"AUTOSWE_FIXTURE_REPO": "https://github.com/acme/fixture"})
    assert len(s.tasks) >= 3
    assert all(t.verify is not None for t in s.tasks), "a shipped task with no verify"
    assert all(t.budget_usd <= 5 for t in s.tasks), "a shipped task that could get expensive"
