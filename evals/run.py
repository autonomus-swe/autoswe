"""Drive a suite through the control plane and record what happened, per task.

These are the numbers the project quotes about itself, so they come from runs rather than
from anyone's recollection. One row per task, appended, with the conditions attached — a
percentage without its model, its date and its cost per task is not a result.

## Through the API, not around it

A task is a `POST /runs` like any other. Nothing here imports the orchestrator, builds a
provider or touches the database. An eval harness that reached inside would be measuring a
path no user takes, and the first thing it would stop noticing is a broken API.

## Resolved means a command exited zero

On a checkout of the branch the agent pushed, not on its diff. A patch that applies
cleanly and fails its own tests is not a resolved task, and only running them tells the
two apart. A task with no verify command is recorded `resolved: null` — never `false`, and
never `true`. A suite that counted unverifiable tasks as successes would report a hundred
per cent the moment somebody forgot one.

## Failures are rows

A task that crashed, timed out, or parked forever on a question gets a row saying so. The
outcome most worth having in the record is the one nobody wants to write down.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from evals import record
from evals.suite import Suite, Task
from observability.logging import get_logger

log = get_logger(__name__)

# Arms that are a property of the *run*, so one suite can be compared against another
# without restarting anything. Each maps to a field the control plane already takes.
#
# `no-repomap` is deliberately absent: the repo map version is a worker process setting
# (`REPO_MAP_VERSION=v1`), not a run field, so that arm is run by restarting the worker.
# Listing it here and quietly ignoring it would produce two identical columns with
# different labels, which is worse than not offering it.
ABLATIONS: dict[str, dict[str, Any]] = {
    "no-debugger": {"budget": {"max_debug_attempts": 0}},
}

POLL_S = 5.0
TERMINAL = frozenset({"done", "failed", "cancelled"})
VERIFY_TIMEOUT_S = 900


@dataclass
class Row:
    """One task's outcome. Every field is read from the run, none is inferred."""

    task_id: str
    suite: str
    tags: list[str] = field(default_factory=list)
    run_id: str = ""
    status: str = ""
    # `None` when the task has no verify command: unverifiable is its own answer, and
    # folding it into either `true` or `false` is how a suite starts lying about itself.
    resolved: bool | None = None
    verify_exit: int | None = None
    verify_output: str = ""
    tasks: int = 0
    debug_attempts: int = 0
    review_rounds: int = 0
    wall_clock_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_hit_rate: float = 0.0
    cost_usd: float = 0.0
    pr_url: str | None = None
    provider: str = ""
    # The arm this row belongs to. Recorded rather than inferred, so `report.compare`
    # groups by a fact about how the run was made instead of a guess.
    ablation: str = ""
    error: str | None = None


Verifier = Callable[[Task, dict[str, Any]], Awaitable[tuple[int, str]]]


class Plane(Protocol):
    """The four calls the driver makes.

    A protocol rather than the concrete `Client` because the driver's job is bookkeeping,
    and bookkeeping should be checkable without a socket. It also says plainly how small
    the harness's dependency on the control plane is: four endpoints, no database, no
    orchestrator.
    """

    async def create(
        self, task: Task, provider: str | None, ablation: str | None = None
    ) -> str: ...

    async def summary(self, run_id: str) -> dict[str, Any]: ...

    async def detail(self, run_id: str) -> dict[str, Any]: ...

    async def cancel(self, run_id: str) -> None: ...


class Client:
    """The control plane over HTTP, with only the four calls this harness makes."""

    def __init__(self, api: str, key: str, *, timeout_s: float = 60.0) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=api.rstrip("/"), headers={"X-API-Key": key}, timeout=timeout_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create(self, task: Task, provider: str | None, ablation: str | None = None) -> str:
        body: dict[str, Any] = {
            "repo_url": task.repo,
            "goal": task.goal,
            "base_branch": task.base,
            "budget": {"max_usd": task.budget_usd},
            # Nobody is watching a suite of thirty. An unattended run refuses approvals and
            # is told not to ask questions, rather than parking until the timeout.
            "unattended": True,
        }
        if provider:
            body["provider"] = provider
        if task.base_commit:
            body["base_commit"] = task.base_commit
        for name, value in ABLATIONS.get(ablation or "", {}).items():
            # Merged rather than replaced: `budget` already carries the task's ceiling,
            # and an arm that reset it would be changing two things and reporting one.
            body[name] = {**body.get(name, {}), **value} if isinstance(value, dict) else value
        response = await self._client.post("/runs", json=body)
        response.raise_for_status()
        return str(response.json()["run_id"])

    async def summary(self, run_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/runs/{run_id}")
        response.raise_for_status()
        return dict(response.json())

    async def detail(self, run_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/runs/{run_id}/detail")
        response.raise_for_status()
        return dict(response.json())

    async def cancel(self, run_id: str) -> None:
        await self._client.post(f"/runs/{run_id}/cancel")


async def wait(client: Plane, run_id: str, timeout_s: float, poll_s: float = POLL_S) -> str:
    """Poll until the run is terminal or the clock runs out. Returns the final status.

    A run that outlives its task's timeout is cancelled rather than abandoned: it holds a
    worker slot, a container and a worktree, and a suite of thirty that leaks one per task
    will not finish.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        status = str((await client.summary(run_id))["status"])
        if status in TERMINAL:
            return status
        if time.monotonic() >= deadline:
            log.warning("eval_task_timed_out", run_id=run_id, status=status)
            await client.cancel(run_id)
            return "timeout"
        await asyncio.sleep(poll_s)


def measure(detail: dict[str, Any]) -> Row:
    """Turn a run's detail into the counted fields of a row.

    Counted from `steps` rather than from an event or a summary field, because a step is
    what actually happened: six Debugger steps is six attempts whatever the phase log says.
    """
    steps = detail.get("steps") or []
    totals = detail.get("totals") or {}
    run = detail.get("run") or {}
    read = int(totals.get("input_tokens") or 0)
    cached = int(totals.get("cache_read_tokens") or 0)
    return Row(
        task_id="",
        suite="",
        run_id=str(run.get("run_id") or ""),
        status=str(run.get("status") or ""),
        tasks=len(detail.get("tasks") or []),
        debug_attempts=sum(1 for s in steps if s.get("agent") == "debugger"),
        review_rounds=sum(1 for s in steps if str(s.get("agent") or "").startswith("review")),
        input_tokens=read,
        output_tokens=int(totals.get("output_tokens") or 0),
        cache_read_tokens=cached,
        cache_hit_rate=round(cached / (read + cached), 4) if read + cached else 0.0,
        cost_usd=float(totals.get("cost_usd") or 0),
        pr_url=run.get("pr_url"),
        error=run.get("error"),
    )


async def checkout_and_verify(task: Task, summary: dict[str, Any]) -> tuple[int, str]:
    """Clone the branch the agent pushed and run the task's command in it.

    The real verifier. `run_task` takes one as an argument so the driver can be tested
    without a network, a fork or a token — and so that a suite against a private host can
    supply its own.
    """
    from repo.gitcmd import git

    if task.verify is None:
        return 0, ""
    branch = str(summary.get("work_branch") or "")
    if not branch:
        return 1, "the run recorded no work branch"

    workspace = Path(tempfile.mkdtemp(prefix="autoswe-eval-"))
    try:
        await git("clone", "--depth", "1", "--branch", branch, task.repo, str(workspace))
        proc = await asyncio.create_subprocess_shell(
            task.verify.command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), VERIFY_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            # Reaped, not just signalled. `kill` sends SIGKILL and returns; without the
            # wait the child is left unreaped and its transport is finalised after the
            # event loop has closed, which raises "Event loop is closed" out of
            # `__del__` — a warning per timed-out task, from a place that has nothing to
            # do with the task. Measured: killing alone reproduces it, `kill` plus this
            # wait does not.
            await proc.wait()
            return 124, f"verify timed out after {VERIFY_TIMEOUT_S}s"
        return proc.returncode or 0, out.decode(errors="replace")[-4000:]
    except Exception as e:  # a clone that fails is a task that did not resolve
        return 1, f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


async def run_task(
    client: Plane,
    task: Task,
    *,
    verifier: Verifier,
    provider: str | None = None,
    ablation: str | None = None,
) -> Row:
    """One task end to end: start it, wait for it, verify it, and say what happened."""
    started = time.monotonic()
    row = Row(
        task_id=task.id,
        suite=task.suite,
        tags=list(task.tags),
        provider=provider or "",
        ablation=ablation or "",
    )
    try:
        run_id = await client.create(task, provider, ablation)
        row.run_id = run_id
        row.status = await wait(client, run_id, task.timeout_s)

        measured = measure(await client.detail(run_id))
        for name, value in asdict(measured).items():
            if name not in ("task_id", "suite", "tags", "provider", "ablation", "status"):
                setattr(row, name, value)
        row.status = row.status or measured.status

        if task.verify is None:
            row.resolved = None
        elif row.status != "done":
            # A run that did not finish has nothing to verify, and cloning a branch that
            # may not exist would report a git error as a test failure.
            row.resolved = False
        else:
            exit_code, output = await verifier(task, await client.summary(run_id))
            row.verify_exit = exit_code
            row.verify_output = output[-4000:]
            row.resolved = exit_code == task.verify.expect_exit
    except Exception as e:
        row.status = row.status or "error"
        row.resolved = False if task.verify is not None else None
        row.error = f"{type(e).__name__}: {e}"
        log.error("eval_task_failed", task=task.id, error=row.error)
    row.wall_clock_s = round(time.monotonic() - started, 1)
    return row


async def run_suite(
    suite: Suite,
    *,
    api: str,
    key: str,
    concurrency: int = 1,
    provider: str | None = None,
    ablation: str | None = None,
    verifier: Verifier | None = None,
    results_name: str | None = None,
) -> list[Row]:
    """Every task in the suite, bounded, with a row appended as each one finishes.

    Appended as they finish rather than at the end: a suite is an hour of real runs, and a
    harness that loses all of it to a crash on the last task is one nobody will run twice.
    """
    verify = verifier or checkout_and_verify
    name = results_name or f"eval-{suite.name}"
    client = Client(api, key)
    limit = asyncio.Semaphore(max(1, concurrency))
    rows: list[Row] = []

    async def one(task: Task) -> Row:
        async with limit:
            log.info("eval_task_started", task=task.id, suite=suite.name)
            row = await run_task(
                client, task, verifier=verify, provider=provider, ablation=ablation
            )
            record.append(name, asdict(row))
            log.info("eval_task_finished", task=task.id, resolved=row.resolved, status=row.status)
            return row

    try:
        rows = list(await asyncio.gather(*(one(t) for t in suite.tasks)))
    finally:
        await client.aclose()
    return rows


async def main() -> int:
    import argparse

    from evals import report
    from evals.suite import load
    from observability.logging import configure_logging

    parser = argparse.ArgumentParser(description="Run an eval suite through the control plane.")
    parser.add_argument("--suite", default="private")
    parser.add_argument("--tags", default="", help="comma-separated; runs only tasks with one")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--provider", default=None, help="override the run provider")
    parser.add_argument(
        "--ablate",
        default=None,
        choices=sorted(ABLATIONS),
        help="run one arm of the ablation; recorded on every row",
    )
    parser.add_argument("--api", default=os.environ.get("AUTOSWE_API", "http://127.0.0.1:8000"))
    parser.add_argument("--results", default=None, help="results file stem")
    args = parser.parse_args()

    configure_logging("INFO")
    key = os.environ.get("AUTOSWE_API_KEY", "")
    if not key:
        print("error: set AUTOSWE_API_KEY")
        return 2

    suite = load(args.suite).tagged(tuple(t for t in args.tags.split(",") if t))
    if not suite.tasks:
        print(f"error: no tasks in suite {args.suite!r} matching {args.tags!r}")
        return 2

    rows = await run_suite(
        suite,
        api=args.api,
        key=key,
        concurrency=args.concurrency,
        provider=args.provider,
        ablation=args.ablate,
        results_name=args.results,
    )
    print(report.render([asdict(r) for r in rows], title=f"Suite: {suite.name}"))
    # Non-zero when anything went unresolved, so this is usable in CI without a wrapper
    # that greps the table.
    return 0 if all(r.resolved is not False for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
