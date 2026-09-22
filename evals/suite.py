"""The task suites: what the agent is asked to do, and how a task counts as resolved.

A task is a goal against a repository plus a command that decides the answer. The command
is the whole point — "resolved" has to mean a test suite passed on the branch the run
produced, not that the model said it was finished, and not that a reviewer skimmed the
diff. Everything else in a result row is context for that one boolean.

## Tasks are validated before any run starts

A suite of thirty tasks costs real money and an hour. A goal the API will reject with a
422 on task twenty-nine should be caught before task one, so every check the control plane
performs is performed here first.

## Repositories come from the environment

The shipped tasks name `${AUTOSWE_FIXTURE_REPO}` rather than somebody's fork. The agent
pushes a branch and opens a pull request, so a task has to point at a repository the
operator controls; a URL committed here would either be wrong for everyone or an invitation
to run an agent against a repository they do not own.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.envsubst import expand
from core.errors import ConfigError

TASKS_DIR = Path(__file__).parent / "tasks"
# The API's own floor (`RunCreate.goal`, min_length=10). Checked here so a suite fails
# while it is still a file rather than on the twenty-ninth POST.
MIN_GOAL = 10
MAX_GOAL = 4000
DEFAULT_BUDGET_USD = 10.0
DEFAULT_TIMEOUT_S = 3600.0


@dataclass(frozen=True)
class Verify:
    """The command that decides whether a task was resolved.

    Run against a checkout of the branch the agent produced, not against its diff: a patch
    that applies cleanly and fails its own tests is not a resolved task, and only running
    them can tell the two apart.
    """

    command: str
    expect_exit: int = 0


@dataclass(frozen=True)
class Task:
    id: str
    repo: str
    goal: str
    base: str = "main"
    verify: Verify | None = None
    tags: tuple[str, ...] = ()
    budget_usd: float = DEFAULT_BUDGET_USD
    timeout_s: float = DEFAULT_TIMEOUT_S
    suite: str = "private"


@dataclass(frozen=True)
class Suite:
    name: str
    tasks: tuple[Task, ...] = field(default_factory=tuple)

    def tagged(self, tags: tuple[str, ...]) -> Suite:
        """The subset carrying any of `tags`. Empty `tags` means all of them."""
        if not tags:
            return self
        chosen = tuple(t for t in self.tasks if set(t.tags) & set(tags))
        return Suite(name=self.name, tasks=chosen)


def load(
    name: str = "private",
    *,
    directory: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Suite:
    """Every task in `<directory>/<name>/*.yaml`, validated.

    A missing directory is an error rather than an empty suite: "the suite ran and
    resolved none of nothing" and "you named a suite that does not exist" are different
    outcomes, and a harness that reports 0/0 as a result has told you nothing.
    """
    root = (directory or TASKS_DIR) / name
    if not root.is_dir():
        raise ConfigError(f"no such suite: {root}")
    env = environ if environ is not None else os.environ

    files = sorted(root.glob("*.yaml"))
    if not files:
        raise ConfigError(f"suite {name!r} has no tasks in {root}")

    tasks = [_one(path, env, name) for path in files]
    if duplicates := sorted({t.id for t in tasks if [x.id for x in tasks].count(t.id) > 1}):
        raise ConfigError(f"suite {name!r} has duplicate task ids: {duplicates}")
    return Suite(name=name, tasks=tuple(tasks))


def _one(path: Path, environ: Mapping[str, str], suite: str) -> Task:
    import yaml

    where = str(path)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{where} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a task must be a mapping")

    task_id = str(raw.get("id") or path.stem)
    goal = str(raw.get("goal") or "").strip()
    if not MIN_GOAL <= len(goal) <= MAX_GOAL:
        raise ConfigError(
            f"{where}: `goal` must be between {MIN_GOAL} and {MAX_GOAL} characters; the "
            "control plane rejects anything else, and finding that out on the last task "
            "of a suite is an hour wasted"
        )
    repo = _repo(expand(str(raw.get("repo") or ""), environ, where=f"{where}: `repo`"), where)
    return Task(
        id=task_id,
        repo=repo,
        goal=goal,
        base=str(raw.get("base") or "main"),
        verify=_verify(raw.get("verify"), where),
        tags=tuple(_str_list(raw.get("tags"), f"{where}: `tags`")),
        budget_usd=float(raw.get("budget_usd", DEFAULT_BUDGET_USD)),
        timeout_s=float(raw.get("timeout_s", DEFAULT_TIMEOUT_S)),
        suite=suite,
    )


def _repo(url: str, where: str) -> str:
    """The same rule `RunCreate.only_github` applies, applied earlier."""
    parsed = urlparse(url.strip())
    owner_repo = [p for p in parsed.path.split("/") if p]
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(owner_repo) < 2:
        raise ConfigError(f"{where}: `repo` must be an https://github.com/owner/name URL")
    return url.strip()


def _verify(raw: Any, where: str) -> Verify | None:
    """`None` is allowed and means the task is scored by a human.

    Not by the absence of a failure: a task with no verify command is reported with
    `resolved: null`, never `resolved: true`, because a suite that counts unverifiable
    tasks as successes is a suite that will report 100 % the moment somebody forgets one.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or not str(raw.get("command") or "").strip():
        raise ConfigError(f"{where}: `verify` needs a `command`")
    return Verify(command=str(raw["command"]).strip(), expect_exit=int(raw.get("expect_exit", 0)))


def _str_list(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{where} must be a list of strings")
    return list(raw)
