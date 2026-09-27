"""Every field on an eval `Task` either reaches the run or is used here, and is asserted.

`tests/integration/test_request_columns_are_read.py` guards the same defect class one layer
down: a `runs` column written by the API, returned by the API, and consulted by nothing. It
was written after that happened four times. This is the guard for the layer above it —
a field in a task YAML, parsed into `Task`, and then dropped.

Five mutations survived the whole suite here, and the shape is identical every time:

| field | what dropping it does |
|---|---|
| `goal` | the run pursues an empty goal; the suite still reports a row |
| `base` | the run works off `main` whatever the YAML declared |
| `base_commit` | **a benchmark pin is silently ignored** and the patch is against the wrong tree |
| `verify.expect_exit` | a "this must keep failing" task is scored by the wrong rule |
| `tags` | `--tags` selects a different subset than the one asked for |

`test_the_request_the_harness_actually_posts` exists and asserts four fields — `unattended`,
`budget`, `provider`, `repo_url`. It reads as a test of the request and is a test of four
fields of it, which is how `goal` and `base` went unchecked for the whole phase.

The other half of why they went unchecked: the shared `task()` helper builds `base="main"`,
which is `Task.base`'s own default. An assertion on that value holds just as well for a
`create` that dropped the field entirely. So every value below is chosen to differ from its
default — that is the single rule that makes this file work.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest

from core.errors import ConfigError
from evals import run as evals_run
from evals import suite as suite_mod
from evals.suite import Task, Verify

pytestmark = pytest.mark.unit

# Deliberately different from every default on `Task`, so that a dropped field cannot pass
# by coinciding with one.
ASKED: dict[str, Any] = {
    "id": "a-distinctive-task-id",
    "repo": "https://github.com/acme/distinctive-repo",
    "goal": "A goal distinctive enough that an empty default could not be mistaken for it.",
    "base": "not-main",
    "base_commit": "a" * 40,
    "verify": Verify(command="uv run pytest -q tests/test_ops.py", expect_exit=3),
    "tags": ("bugfix", "fixture"),
    "budget_usd": 4.25,
    "timeout_s": 601.0,
    "suite": "not-private",
}

# Field -> where it must appear in the POST body, and how to read it back out.
IN_BODY: dict[str, tuple[str, Any]] = {
    "repo": ("repo_url", lambda b: b["repo_url"]),
    "goal": ("goal", lambda b: b["goal"]),
    "base": ("base_branch", lambda b: b["base_branch"]),
    "base_commit": ("base_commit", lambda b: b["base_commit"]),
    "budget_usd": ("budget", lambda b: b["budget"]["max_usd"]),
}

# Fields the run never sees, because they are the harness's own business. Each carries the
# reason, so "I could not think of an assertion" cannot hide in here — and each is asserted
# by a named test below, because a field classified as local-only and then never used
# locally is the same bug wearing a different label.
LOCAL_ONLY: dict[str, str] = {
    "id": "names the result row; asserted by test_the_local_only_fields_are_used_locally",
    "verify": "run against the agent's branch after the fact, never by the run itself",
    "tags": "selects the subset of the suite to run; the run has no use for them",
    "timeout_s": "how long this harness waits before cancelling; not a run field",
    "suite": "recorded on the result row so a mixed results file can be read apart",
}


def full_task() -> Task:
    return Task(**ASKED)


async def post(task: Task, provider: str | None = "openai_compat") -> dict[str, Any]:
    """The body the real `Client` posts. The real one, not a fake.

    A fake standing in for `create` would be asserting its own body-building, which is
    exactly the code under suspicion.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "r1"})

    client = evals_run.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        await client.create(task, provider)
    finally:
        await client.aclose()
    return captured


def test_every_task_field_is_either_sent_or_local() -> None:
    """Adding a field to `Task` forces a decision about which it is.

    The five bugs were not wrong decisions; they were absent ones. Nobody chose to drop
    `base_commit` — it was added to `Task`, threaded into `create`, and the question of
    whether anything asserted its arrival was never asked.
    """
    fields = {f.name for f in dataclasses.fields(Task)}
    unclassified = fields - set(IN_BODY) - set(LOCAL_ONLY)
    assert unclassified == set(), (
        f"new Task fields {sorted(unclassified)}: add each to IN_BODY (with the body key "
        "and a distinctive value in ASKED) or to LOCAL_ONLY (with a reason)"
    )
    assert not (set(IN_BODY) & set(LOCAL_ONLY)), "a field cannot be both"
    assert set(ASKED) == fields, "ASKED must name every field, so none is left at its default"


def test_no_asked_value_equals_its_own_default() -> None:
    """The rule that makes this file mean anything.

    `base="main"` is `Task.base`'s default, and the shared `task()` helper used it — so an
    assertion on `base_branch == "main"` passed on a `create` that never sent the field.
    Any future value added to ASKED that happens to equal its default would reintroduce
    exactly that hole, silently, so it is checked rather than remembered.
    """
    defaults = {
        f.name: f.default for f in dataclasses.fields(Task) if f.default is not dataclasses.MISSING
    }
    same = {k: v for k, v in defaults.items() if k in ASKED and ASKED[k] == v}
    assert same == {}, f"these ASKED values equal their defaults and prove nothing: {same}"


@pytest.mark.parametrize("field", sorted(IN_BODY))
async def test_a_task_field_reaches_the_post_body(field: str) -> None:
    """Parametrised so a failure names the field rather than "the request".

    "the request was wrong" is true of five separate surviving mutations; "`base_commit`
    never left the harness" is the one that gets fixed.
    """
    body = await post(full_task())
    key, extract = IN_BODY[field]
    assert key in body, f"{field} should be posted as {key!r}; body had {sorted(body)}"
    assert extract(body) == ASKED[field]


async def test_a_task_that_pins_no_commit_omits_the_field_rather_than_nulling_it() -> None:
    """The other direction, so the test above cannot pass by hard-coding.

    An ordinary task does not pin a commit, and `base_commit: null` is not the same request
    as no `base_commit` at all — the API would have to decide what a null pin means.
    """
    body = await post(dataclasses.replace(full_task(), base_commit=None))
    assert "base_commit" not in body


async def test_a_task_of_defaults_produces_a_body_of_defaults() -> None:
    """The counterweight to every assertion above.

    If `create` hard-coded the values this file asks for, all of them would hold and
    nothing would work. A task that asked for nothing optional must come back with
    nothing optional.
    """
    plain = Task(
        id="plain", repo="https://github.com/acme/plain", goal="A plain goal, long enough."
    )
    body = await post(plain, provider=None)
    assert body["base_branch"] == "main"
    assert "base_commit" not in body
    assert "provider" not in body
    assert body["goal"] == "A plain goal, long enough."


# ---- the hop above: YAML into a Task ------------------------------------------------------
#
# The first version of this file guarded `Task -> POST body` and stopped there, and four of
# the five mutations went straight through it: they are in `evals/suite._one`, which turns
# the YAML into the `Task` in the first place. Guarding the second hop while the first was
# open is exactly the mistake this whole file is about, one layer up. The chain is
# `YAML -> Task -> POST body` and all three links need a test.


def write_suite(root: Any, body: str, name: str = "demo") -> Any:
    directory = root / "tasks" / "aSuite"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(body)
    return root / "tasks"


FULL_YAML = f"""
id: {ASKED["id"]}
repo: {ASKED["repo"]}
goal: >-
  {ASKED["goal"]}
base: {ASKED["base"]}
base_commit: {ASKED["base_commit"]}
verify:
  command: "{ASKED["verify"].command}"
  expect_exit: {ASKED["verify"].expect_exit}
tags: [{", ".join(ASKED["tags"])}]
budget_usd: {ASKED["budget_usd"]}
timeout_s: {ASKED["timeout_s"]}
"""


@pytest.mark.parametrize("field", sorted(set(ASKED) - {"suite"}))
def test_a_yaml_field_reaches_the_task(field: str, tmp_path: Any) -> None:
    """Every field a task file can declare survives being loaded.

    Parametrised for the same reason as the POST-body test: "the loader is wrong" was true
    of four separate surviving mutations, each of which quietly replaced a parsed value
    with its own default — `base="main"`, `base_commit=None`, `expect_exit=0`.

    `suite` is excluded because the loader sets it from the directory name rather than from
    the file, and the test below covers that.
    """

    loaded = suite_mod.load("aSuite", directory=write_suite(tmp_path, FULL_YAML))
    (task,) = loaded.tasks
    assert getattr(task, field) == ASKED[field]


def test_the_suite_name_comes_from_the_directory_not_the_file(tmp_path: Any) -> None:
    """The one field the file does not own. Recorded on every result row, so a results file
    holding two suites can be read apart afterwards."""

    (task,) = suite_mod.load("aSuite", directory=write_suite(tmp_path, FULL_YAML)).tasks
    assert task.suite == "aSuite"


def test_a_scalar_tags_is_refused_rather_than_becoming_one_character_per_tag(
    tmp_path: Any,
) -> None:
    """`tags: bugfix` instead of `tags: [bugfix]` is an ordinary thing to write.

    Without the type guard a bare string is accepted and iterated, so the task ends up
    tagged `b`, `u`, `g`, `f`, `i`, `x` — and `--tags bugfix` then selects nothing, with no
    error anywhere. A suite that silently runs zero tasks and reports 0/0.

    Disarming that guard (`if False:`) survived the whole suite.
    """

    bad = FULL_YAML.replace("tags: [bugfix, fixture]", "tags: bugfix")
    with pytest.raises(ConfigError, match="list of strings"):
        suite_mod.load("aSuite", directory=write_suite(tmp_path, bad))


async def test_the_pinned_commit_and_declared_base_survive_all_the_way_to_the_request(
    tmp_path: Any,
) -> None:
    """The whole chain in one assertion, because the links were tested separately and the
    joint is what broke.

    A benchmark instance pins a commit in its YAML; if either hop drops it, the agent works
    from the head of a branch and the patch does not apply to the instance's base — the
    failure `RunCreate.base_commit` was added to prevent, reported as the agent's.
    """

    (task,) = suite_mod.load("aSuite", directory=write_suite(tmp_path, FULL_YAML)).tasks

    body = await post(task)
    assert body["base_commit"] == ASKED["base_commit"]
    assert body["base_branch"] == ASKED["base"]
    assert body["goal"] == ASKED["goal"]


def test_the_local_only_fields_are_used_locally() -> None:
    """A field classified as local-only and then used nowhere is the same bug relabelled.

    So each one is named against the line that consumes it. These are read off `evals/run`
    rather than asserted behaviourally because three of them are consumed by `run_task`,
    which the tests above deliberately do not drive — but a field that appears in no source
    line at all is caught here rather than passing as "local".
    """
    source = (evals_run.__file__ and open(evals_run.__file__).read()) or ""
    for field in LOCAL_ONLY:
        assert f"task.{field}" in source, (
            f"`{field}` is classified LOCAL_ONLY but `task.{field}` appears nowhere in "
            "evals/run.py — it is not local, it is dropped"
        )


async def test_the_verify_rule_a_task_declared_is_the_rule_it_is_scored_by() -> None:
    """`expect_exit` is the field a "this must keep failing" task depends on entirely.

    Scored against a hard-coded 0, such a task reads as unresolved however correctly the
    agent behaved; scored against a hard-coded non-zero, a normal task reads as resolved
    when its tests failed. Both are the report lying in the direction nobody checks.
    """
    task = dataclasses.replace(full_task(), verify=Verify(command="pytest -q", expect_exit=3))

    class Plane:
        async def create(self, t: Any, p: Any, a: Any = None) -> str:
            return "r1"

        async def summary(self, run_id: str) -> dict[str, Any]:
            return {"run_id": run_id, "status": "done"}

        async def detail(self, run_id: str) -> dict[str, Any]:
            return {"run": {"run_id": run_id, "status": "done"}, "steps": [], "totals": {}}

        async def cancel(self, run_id: str) -> None:
            return None

    async def verifier(t: Task, summary: dict[str, Any]) -> tuple[int, str]:
        # Exits 3, which is exactly what the task above declared it expects. A harness
        # comparing against anything else scores this unresolved.
        return 3, "1 failed as designed"

    row = await evals_run.run_task(Plane(), task, verifier=verifier)
    assert row.resolved is True, "a task declaring expect_exit=3 and getting 3 is resolved"

    strict = dataclasses.replace(task, verify=Verify(command="pytest -q", expect_exit=0))
    row = await evals_run.run_task(Plane(), strict, verifier=verifier)
    assert row.resolved is False, "the same exit code against expect_exit=0 is not resolved"
