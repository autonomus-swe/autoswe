"""The verifier, which is what every "resolved" in `docs/numbers.md` actually means.

`docs/evals.md` §1 defines resolved as *"a command exited zero on a checkout of the branch
the agent pushed"*. `checkout_and_verify` is that sentence in code, and four mutations in it
survived the whole suite:

| mutation | what the report would then say |
|---|---|
| drop `--branch` from the clone | scored against the **default branch**, not the agent's work |
| `proc.returncode or 0` → `0` | **everything resolves**, including tasks whose tests failed |
| `Client.cancel` POST → GET | a timed-out run is never cancelled and keeps its worker slot |
| `ablation=args.ablate` → `None` | an arm runs as the baseline and is reported as the arm |

The second is the one that matters most and it is the easiest to miss: nothing about a
suite that reports 3/3 looks wrong. `test_the_denominator_is_what_could_be_verified` and
friends all pass a *scripted* verifier, so every existing test of the eval driver asserts
the driver's arithmetic and none of them ever runs the real thing. The gap was not a missing
assertion, it was a missing subject.

The last one has already happened once in a neighbouring form: `--ablate` existed on the
driver and not on `autoswe eval`, so the documented command exited 2. That was fixed by
adding the flag and testing that the two surfaces agree — and the line that passes it on to
`run_suite` still had nothing checking it.

These use a real local git repository and a real subprocess. No network, no fork, no token:
`git clone` works perfectly well against a path on disk, which is what makes the real
verifier testable at all.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import run as evals_run
from evals.suite import Task, Verify

pytestmark = pytest.mark.unit


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    )


def origin_with_two_branches(root: Path) -> Path:
    """A repository whose `main` fails the check and whose agent branch passes it.

    That asymmetry is the whole point. If both branches passed, dropping `--branch` would
    still produce a green verify and the test would be measuring nothing — which is exactly
    how this gap survived: the fixture repository's branches were never made to disagree.
    """
    origin = root / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)

    (origin / "check.sh").write_text("#!/bin/sh\nexit 7\n")  # main: fails, distinctively
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "main is red", cwd=origin)

    git("checkout", "-q", "-b", "agent/run-1", cwd=origin)
    (origin / "check.sh").write_text("#!/bin/sh\nexit 0\n")  # the agent's branch: passes
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "the agent fixed it", cwd=origin)
    git("checkout", "-q", "main", cwd=origin)
    return origin


def task_for(origin: Path, expect_exit: int = 0) -> Task:
    return Task(
        id="verify-me",
        repo=str(origin),
        goal="A goal long enough to satisfy the loader's floor.",
        verify=Verify(command="sh check.sh", expect_exit=expect_exit),
    )


# ---- the branch ---------------------------------------------------------------------------


async def test_the_verifier_checks_out_the_branch_the_agent_pushed(tmp_path: Path) -> None:
    """The definition of "resolved", asserted against a repository where the two branches
    disagree.

    `main` exits 7 and `agent/run-1` exits 0. A verifier that clones the default branch
    reports 7 and the task reads unresolved; the agent's work is never looked at. Dropping
    `--branch` from the clone survived the entire suite.
    """
    origin = origin_with_two_branches(tmp_path)
    code, output = await evals_run.checkout_and_verify(
        task_for(origin), {"work_branch": "agent/run-1"}
    )
    assert code == 0, f"the agent's branch passes; got {code} with output {output!r}"


async def test_a_verifier_pointed_at_the_wrong_branch_reports_that_branchs_result(
    tmp_path: Path,
) -> None:
    """The control that makes the test above mean something.

    If the fixture's branches did not disagree, both assertions would hold on a verifier
    that ignored the branch entirely. Asking for `main` explicitly must produce main's
    distinctive exit code.
    """
    origin = origin_with_two_branches(tmp_path)
    code, _ = await evals_run.checkout_and_verify(task_for(origin), {"work_branch": "main"})
    assert code == 7, "main is red on purpose; this is the value a branch-blind clone returns"


async def test_a_run_with_no_work_branch_is_not_quietly_resolved(tmp_path: Path) -> None:
    """There is nothing to check out, so there is nothing to score. Returning 0 here would
    resolve a task whose run never got as far as pushing."""
    origin = origin_with_two_branches(tmp_path)
    code, output = await evals_run.checkout_and_verify(task_for(origin), {})
    assert code != 0
    assert "work branch" in output


async def test_a_branch_that_does_not_exist_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    """A clone that fails is a task that did not resolve. The `except` around the clone
    returns 1 rather than raising, so a single bad task cannot end a suite of thirty — but
    it must not return 0 either."""
    origin = origin_with_two_branches(tmp_path)
    code, output = await evals_run.checkout_and_verify(
        task_for(origin), {"work_branch": "agent/never-pushed"}
    )
    assert code != 0, "a missing branch resolved nothing"
    assert output, "and it says why"


# ---- the exit code ------------------------------------------------------------------------


async def test_the_exit_code_comes_from_the_process_that_ran(tmp_path: Path) -> None:
    """`return 0` in place of `proc.returncode` makes every task resolve.

    A suite that reports 3/3 looks exactly the same either way, which is why this needs a
    real subprocess with a real non-zero exit rather than a scripted verifier. Every other
    test of this driver supplies its own verifier and so never runs this line.
    """
    origin = origin_with_two_branches(tmp_path)
    task = Task(
        id="failing",
        repo=str(origin),
        goal="A goal long enough to satisfy the loader's floor.",
        verify=Verify(command="sh -c 'exit 3'"),
    )
    code, _ = await evals_run.checkout_and_verify(task, {"work_branch": "agent/run-1"})
    assert code == 3, "the verifier must report what the command did, not what we hoped"


async def test_the_commands_output_travels_with_its_exit_code(tmp_path: Path) -> None:
    """The `why` column in the report comes from here. An exit code with no output is a
    failure nobody can act on without re-running the suite."""
    origin = origin_with_two_branches(tmp_path)
    task = Task(
        id="noisy",
        repo=str(origin),
        goal="A goal long enough to satisfy the loader's floor.",
        verify=Verify(command="sh -c 'echo DISTINCTIVE_MARKER; exit 1'"),
    )
    code, output = await evals_run.checkout_and_verify(task, {"work_branch": "agent/run-1"})
    assert code == 1
    assert "DISTINCTIVE_MARKER" in output


async def test_a_task_with_nothing_to_verify_returns_zero_without_cloning(
    tmp_path: Path,
) -> None:
    """`resolved: null` is decided by `run_task` from `task.verify is None`; this is the
    other half — the verifier must not do work, or fail, for a task that declares none."""
    origin = origin_with_two_branches(tmp_path)
    task = Task(
        id="unverifiable",
        repo=str(origin),
        goal="A goal long enough to satisfy the loader's floor.",
        verify=None,
    )
    assert await evals_run.checkout_and_verify(task, {"work_branch": "nonexistent"}) == (0, "")


# ---- cancelling ----------------------------------------------------------------------------


async def test_cancel_posts_to_the_cancel_endpoint(tmp_path: Path) -> None:
    """A run past its timeout holds a worker slot, a container and a worktree.

    `wait` cancels rather than abandoning it, for that reason — and `Client.cancel` is the
    line that does it. Sent as a GET the control plane answers 405 and the run keeps
    running, and because nothing here raises, a suite of thirty leaks one per timeout while
    reporting normally. Swapping the verb survived the whole suite.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(202, json={})

    client = evals_run.Client("http://x", "k")
    client._client = httpx.AsyncClient(base_url="http://x", transport=httpx.MockTransport(handler))
    try:
        await client.cancel("r1")
    finally:
        await client.aclose()

    assert seen["method"] == "POST", "a GET is a 405 and a run that is still going"
    assert seen["path"] == "/runs/r1/cancel"


# ---- the arm reaches the suite ------------------------------------------------------------


async def test_the_ablation_arm_reaches_run_suite_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same call-site gap that already shipped once on this flag.

    `--ablate` was added to the driver and not to `autoswe eval`, so the documented command
    exited 2. That was fixed and tested. The line handing the parsed value to `run_suite`
    was not, so `ablation=None` at the call site still left the suite green — an arm that
    runs as the baseline and is labelled as the arm, which is worse than not running it.
    """
    seen: dict[str, Any] = {}

    async def fake_run_suite(suite: Any, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(evals_run, "run_suite", fake_run_suite)
    # `main` does `from evals.suite import load` at call time, so the patch belongs on the
    # module it is imported from rather than on `evals.run`.
    monkeypatch.setattr("evals.suite.load", _one_task_suite)
    monkeypatch.setenv("AUTOSWE_API_KEY", "k")
    monkeypatch.setattr(
        "sys.argv",
        ["run.py", "--suite", "private", "--ablate", "no-debugger", "--results", "an-arm"],
    )

    await evals_run.main()

    assert seen.get("ablation") == "no-debugger", "the arm never reached the suite"
    assert seen.get("results_name") == "an-arm", "and neither did the file it writes to"


def _one_task_suite(name: str) -> Any:
    from evals.suite import Suite

    return Suite(
        name=name,
        tasks=(
            Task(
                id="t",
                repo="https://github.com/acme/fixture",
                goal="A goal long enough to satisfy the loader's floor.",
            ),
        ),
    )


async def test_an_absent_arm_is_none_rather_than_a_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, so the test above cannot pass on a hard-coded arm name. A
    baseline run must be recorded as a baseline."""
    seen: dict[str, Any] = {}

    async def fake_run_suite(suite: Any, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(evals_run, "run_suite", fake_run_suite)
    monkeypatch.setattr("evals.suite.load", _one_task_suite)
    monkeypatch.setenv("AUTOSWE_API_KEY", "k")
    monkeypatch.setattr("sys.argv", ["run.py", "--suite", "private"])

    await evals_run.main()
    assert seen.get("ablation") is None


# ---- the timeout --------------------------------------------------------------------------


async def test_a_verify_command_that_hangs_is_killed_rather_than_waited_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verify command is arbitrary shell from a task file, so it can hang.

    `VERIFY_TIMEOUT_S` is fifteen minutes in production, which is not a thing a unit test
    can wait for, so the constant is lowered rather than the behaviour faked. 124 is the
    conventional timeout code and the report shows it as the reason.

    **Do not wrap this call in `asyncio.wait_for`.** An inner `wait_for` that times out
    cancels the current task; awaiting anything in the resulting `except` block while an
    *outer* `wait_for` is still active lets that cancellation be caught by the outer scope
    and re-raised as its `TimeoutError`. Production never nests — `run_task` awaits the
    verifier directly and `run_suite` uses `gather` — so the nesting only ever existed here,
    as belt-and-braces that turned a correct fix into a red test. The lowered
    `VERIFY_TIMEOUT_S` is what bounds this test.
    """
    origin = origin_with_two_branches(tmp_path)
    monkeypatch.setattr(evals_run, "VERIFY_TIMEOUT_S", 0.5)
    task = Task(
        id="hangs",
        repo=str(origin),
        goal="A goal long enough to satisfy the loader's floor.",
        verify=Verify(command="sleep 30"),
    )
    code, output = await evals_run.checkout_and_verify(task, {"work_branch": "agent/run-1"})

    assert code == 124
    assert "timed out" in output


async def test_the_killed_process_is_reaped_rather_than_left_a_zombie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kill` sends the signal and returns; it does not wait for the child to die.

    Left unreaped, the child's transport is finalised after the event loop has closed and
    raises "Event loop is closed" out of `__del__` — once per timed-out task, from a place
    that has nothing to do with the task.

    Asserted on `returncode` rather than on the warning. The warning is emitted at
    collection time, which is usually after the test that caused it, so
    `filterwarnings("error::…Unraisable…")` does **not** fail this test when the reap is
    removed — measured, not assumed. `returncode` is `None` until the child is reaped and
    the signal number afterwards, which is the same fact available synchronously.
    """
    origin = origin_with_two_branches(tmp_path)
    monkeypatch.setattr(evals_run, "VERIFY_TIMEOUT_S", 0.5)
    spawned: list[Any] = []

    real_spawn = asyncio.create_subprocess_shell

    async def recording_spawn(cmd: str, **kwargs: Any) -> Any:
        proc = await real_spawn(cmd, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", recording_spawn)

    task = Task(
        id="hangs",
        repo=str(origin),
        goal="A goal long enough to satisfy the loader's floor.",
        verify=Verify(command="sleep 30"),
    )
    code, _ = await evals_run.checkout_and_verify(task, {"work_branch": "agent/run-1"})

    assert code == 124
    assert spawned, "the verify command was never started"
    returncode = spawned[0].returncode
    assert returncode is not None, (
        "the killed child was never reaped; its transport will be finalised after the "
        "event loop closes and raise out of __del__"
    )
    assert returncode < 0, (
        f"returncode {returncode} means the command ran to completion — it was reaped but "
        "never killed, so a verify command that truly hangs blocks the suite for as long "
        "as it likes, which is the thing the timeout exists to prevent. A negative code is "
        "death by signal."
    )
