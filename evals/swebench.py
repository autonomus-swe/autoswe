"""SWE-bench Lite: drive instances through the control plane, write `predictions.jsonl`.

This produces patches. It does not score them. Scoring is the official harness's job:

    python -m swebench.harness.run_evaluation \
        --predictions_path evals/results/predictions.jsonl --run_id <id>

That split is the plan's and it is right. Every instance needs a specific Python version
and a specific set of pinned dependencies, and the harness has an environment image per
instance to provide them. Making this project's sandbox images solve that problem would be
reimplementing SWE-bench in order to run SWE-bench, and the score would be against our
reconstruction of the environment rather than against the benchmark.

## A run per instance, through the API

`instance_id` becomes the task, `problem_statement` becomes the goal, and the `diff`
artifact the run wrote becomes `model_patch`. Nothing here reaches into the orchestrator,
for the same reason `evals/run.py` does not.

## `base_commit` is not yet honoured

Each instance pins a commit, and `RunCreate` takes a branch. The plan says to extend it to
accept a SHA; until it does, a run starts from the branch head and a patch may not apply
to the instance's base. `--require-sha` refuses to produce predictions rather than writing
a file that will score badly for a reason the score cannot show — a low number nobody can
attribute is worse than no number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.run import Client, measure, wait
from evals.suite import Task
from observability.logging import get_logger

log = get_logger(__name__)

DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_BUDGET_USD = 3.0
DEFAULT_TIMEOUT_S = 2700.0
MODEL_NAME = "autoswe"


@dataclass(frozen=True)
class Instance:
    instance_id: str
    repo: str  # "owner/name" as the dataset gives it
    base_commit: str
    problem_statement: str

    def repo_url(self, fork_owner: str | None) -> str:
        """Where the run should clone from.

        A fork when one is given, because the agent pushes a branch and opens a pull
        request, and it cannot do either against a repository it does not own.
        """
        owner, _, name = self.repo.partition("/")
        return f"https://github.com/{fork_owner or owner}/{name}"

    def task(self, fork_owner: str | None, budget_usd: float, timeout_s: float) -> Task:
        return Task(
            id=self.instance_id,
            repo=self.repo_url(fork_owner),
            goal=self.problem_statement.strip()[:4000],
            base="main",
            verify=None,  # the official harness decides; see the module docstring
            tags=("swebench",),
            budget_usd=budget_usd,
            timeout_s=timeout_s,
            suite="swebench",
        )


def load_instances(limit: int, *, split: str = "test") -> list[Instance]:
    """The first `limit` instances of SWE-bench Lite.

    `datasets` is not a dependency of this project — it brings pyarrow and several hundred
    megabytes — so the import is here and its absence is a sentence telling you what to
    install rather than a traceback.
    """
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise SystemExit(
            "SWE-bench needs the `datasets` package:\n"
            "    uv pip install datasets swebench\n"
            "It is deliberately not a dependency of this project."
        ) from e

    rows = load_dataset(DATASET, split=split)
    return [
        Instance(
            instance_id=str(r["instance_id"]),
            repo=str(r["repo"]),
            base_commit=str(r["base_commit"]),
            problem_statement=str(r["problem_statement"]),
        )
        for r in rows.select(range(min(limit, len(rows))))
    ]


async def patch_for(client: Client, run_id: str) -> str:
    """The run's `diff` artifact, or an empty patch.

    An empty `model_patch` is a legitimate prediction meaning "this instance was not
    solved". Omitting the row instead would shrink the denominator, which is a way of
    improving a score by not reporting the attempts that failed.
    """
    import httpx

    try:
        response = await client._client.get(f"/runs/{run_id}/artifacts/diff")
        if response.status_code == 200:
            return response.text
    except httpx.HTTPError as e:
        log.warning("swebench_diff_unavailable", run_id=run_id, error=str(e))
    return ""


async def predict(
    instances: list[Instance],
    *,
    api: str,
    key: str,
    fork_owner: str | None,
    concurrency: int = 1,
    budget_usd: float = DEFAULT_BUDGET_USD,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """One prediction per instance, in the order they were given."""
    client = Client(api, key)
    limit = asyncio.Semaphore(max(1, concurrency))

    async def one(instance: Instance) -> dict[str, Any]:
        async with limit:
            task = instance.task(fork_owner, budget_usd, timeout_s)
            log.info("swebench_instance_started", instance=instance.instance_id)
            try:
                run_id = await client.create(task, provider)
                status = await wait(client, run_id, task.timeout_s)
                row = measure(await client.detail(run_id))
                patch = await patch_for(client, run_id) if status == "done" else ""
            except Exception as e:
                log.error("swebench_instance_failed", instance=instance.instance_id, error=str(e))
                return _prediction(instance, "", {"error": f"{type(e).__name__}: {e}"})
            return _prediction(
                instance,
                patch,
                {
                    "run_id": run_id,
                    "status": status,
                    "cost_usd": row.cost_usd,
                    "wall_clock_s": row.wall_clock_s,
                    "debug_attempts": row.debug_attempts,
                },
            )

    try:
        return list(await asyncio.gather(*(one(i) for i in instances)))
    finally:
        await client.aclose()


def _prediction(instance: Instance, patch: str, extra: dict[str, Any]) -> dict[str, Any]:
    """The three fields the harness reads, plus ours.

    The harness ignores what it does not know, and cost per resolved instance cannot be
    reconstructed later from a file that only carries patches.
    """
    return {
        "instance_id": instance.instance_id,
        "model_name_or_path": MODEL_NAME,
        "model_patch": patch,
        "autoswe": extra,
    }


def write(predictions: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(p) + "\n" for p in predictions))
    return path


async def main() -> int:
    from observability.logging import configure_logging

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5, help="run five before you run fifty")
    parser.add_argument("--fork-owner", default=os.environ.get("AUTOSWE_FORK_OWNER"))
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--api", default=os.environ.get("AUTOSWE_API", "http://127.0.0.1:8000"))
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--out", type=Path, default=Path("evals/results/predictions.jsonl"))
    parser.add_argument(
        "--require-sha",
        action="store_true",
        help="refuse to run until RunCreate accepts a base commit (see the module docstring)",
    )
    args = parser.parse_args()

    configure_logging("INFO")
    key = os.environ.get("AUTOSWE_API_KEY", "")
    if not key:
        print("error: set AUTOSWE_API_KEY")
        return 2
    if args.require_sha:
        print(
            "error: RunCreate does not accept a base commit yet, so every instance would "
            "start from its branch head rather than its pinned base. Re-run without "
            "--require-sha to produce predictions anyway, and say so beside the score."
        )
        return 2

    instances = load_instances(args.limit)
    predictions = await predict(
        instances,
        api=args.api,
        key=key,
        fork_owner=args.fork_owner,
        concurrency=args.concurrency,
        budget_usd=args.budget_usd,
        provider=args.provider,
    )
    path = write(predictions, args.out)
    solved = sum(1 for p in predictions if p["model_patch"])
    print(f"{len(predictions)} predictions, {solved} with a patch -> {path}")
    print("\nScore them with the official harness:")
    print(f"  python -m swebench.harness.run_evaluation --predictions_path {path} --run_id <id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
