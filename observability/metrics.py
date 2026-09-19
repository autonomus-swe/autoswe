"""The numbers this project quotes about itself, as Prometheus metrics.

A trace tells you about one run. These tell you whether the agent is getting better or
worse across all of them — which is a different question, and the only one that can
justify a change to a prompt or a route.

## Cost per solved task is the metric

Not cost per run. A run that spent half as much and finished one task instead of three
cost more per unit of work, and a total alone hides that completely. The same reasoning
puts `first_pass_test_success_total` and `debug_success_total` side by side: a rise in the
second with no rise in the first is the debug loop covering for a worse Coder, which reads
as success on any dashboard that only counts finished tasks.

## Recording never raises

Every function here is called from a path that matters — the ledger hook, the sandbox, the
end of a run. A metrics library that throws during a label lookup would fail a run over a
number nobody is watching yet, so each entry point is wrapped. There is no case where a
missing observation is worse than a lost run.

## One registry per process

The API and the worker are separate processes with separate registries, which is why each
exposes its own endpoint rather than one aggregating the other. The API's counters are
about requests; the worker's are about runs. A scrape config needs both targets.
"""

from __future__ import annotations

from collections.abc import Iterable

from prometheus_client import Counter, Histogram

from observability.logging import get_logger

log = get_logger(__name__)

# Attempt counts are small integers and the interesting question is "how many needed more
# than one", so the buckets are the integers rather than a spread.
ATTEMPT_BUCKETS = (0, 1, 2, 3, 4, 5)
# Token counts for a solved task span three orders of magnitude between a one-line fix and
# a multi-file change.
TOKEN_BUCKETS = (10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 2_500_000)
# A rate lives in [0, 1]; the low buckets are the ones worth resolving, because the
# difference between 0 % and 10 % is a broken cache and the difference between 80 % and
# 90 % is a good day.
RATE_BUCKETS = (0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 1.0)
# Sandbox commands run from a millisecond `git status` to a ten-minute test suite.
SECOND_BUCKETS = (0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0)

RUNS = Counter("autoswe_runs_total", "Runs that reached a terminal phase.", ["outcome"])
TASK_ATTEMPTS = Histogram(
    "autoswe_task_attempts",
    "Debug attempts spent on each task of a finished run.",
    buckets=ATTEMPT_BUCKETS,
)
FIRST_PASS = Counter(
    "autoswe_first_pass_test_success_total",
    "Tasks whose tests passed with no debugging at all.",
)
DEBUG_SUCCESS = Counter(
    "autoswe_debug_success_total",
    "Tasks that passed after at least one debug attempt.",
)
TOKENS_PER_TASK = Histogram(
    "autoswe_tokens_per_solved_task",
    "Tokens a run spent divided by the tasks it finished.",
    buckets=TOKEN_BUCKETS,
)
CACHE_HIT_RATE = Histogram(
    "autoswe_cache_hit_rate",
    "Per model call: cache reads over everything the model read.",
    buckets=RATE_BUCKETS,
)
SANDBOX_SECONDS = Histogram(
    "autoswe_sandbox_exec_seconds",
    "Wall clock of one command in the sandbox.",
    buckets=SECOND_BUCKETS,
)
COST = Counter("autoswe_cost_usd_total", "Dollars spent, by the role that spent them.", ["role"])


def _safe(what: str, fn: object) -> None:
    """Run a recording call, swallowing anything it raises.

    A number nobody is watching yet must not be able to fail a run that is.
    """
    try:
        fn()  # type: ignore[operator]
    except Exception as e:  # pragma: no cover - defensive
        log.warning("metric_failed", metric=what, error=f"{type(e).__name__}: {e}")


def record_llm_call(role: str, cost_usd: float, cache_hit_rate: float) -> None:
    """One model call, from the ledger hook — the single place every call passes through."""

    def go() -> None:
        COST.labels(role=role).inc(max(0.0, cost_usd))
        CACHE_HIT_RATE.observe(cache_hit_rate)

    _safe("llm_call", go)


def record_sandbox_exec(seconds: float) -> None:
    def go() -> None:
        SANDBOX_SECONDS.observe(max(0.0, seconds))

    _safe("sandbox_exec", go)


def record_task_done(debug_attempts: int) -> None:
    """A task whose tests went green.

    The two counters are deliberately separate rather than one with a label: a rise in
    debug successes with no rise in first-pass successes is the debug loop covering for a
    worse Coder, and that is the shape worth being able to see at a glance.
    """

    def go() -> None:
        (FIRST_PASS if debug_attempts == 0 else DEBUG_SUCCESS).inc()

    _safe("task_done", go)


def record_run_finished(
    *, outcome: str, attempts: Iterable[int], tasks_done: int, total_tokens: int
) -> None:
    """A run reaching a terminal phase.

    Takes plain numbers rather than a `RunState` so this module stays below the
    orchestrator and can be imported from anywhere without a cycle.

    `tokens_per_solved_task` is skipped rather than recorded as zero when a run finished
    nothing: dividing by no tasks is not a large number, it is not a number, and a zero in
    that histogram would drag the average of the runs that did work.
    """

    def go() -> None:
        RUNS.labels(outcome=outcome).inc()
        for count in attempts:
            TASK_ATTEMPTS.observe(count)
        if tasks_done > 0 and total_tokens > 0:
            TOKENS_PER_TASK.observe(total_tokens / tasks_done)

    _safe("run_finished", go)


def render() -> tuple[bytes, str]:
    """`(body, content type)` for whatever is scraping this process."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return generate_latest(), CONTENT_TYPE_LATEST
