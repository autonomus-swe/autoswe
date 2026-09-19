"""The numbers the project quotes about itself.

A trace tells you about one run; these tell you whether the agent is getting better or
worse across all of them. Two things are worth pinning.

**Recording must never raise.** Every entry point is called from a path that matters — the
ledger hook, the sandbox, the end of a run — and a metrics library throwing during a label
lookup would fail a run over a number nobody is watching yet.

**The division has to refuse the case it cannot answer.** A run that finished no tasks has
no cost per task; recording a zero there would drag the average of every run that did work.
"""

from __future__ import annotations

from typing import Any

import pytest
from prometheus_client import REGISTRY

from observability import metrics

pytestmark = pytest.mark.unit


def value(name: str, **labels: str) -> float:
    """The current sample, or 0.0 before anything has been recorded under those labels."""
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


# ---- the ledger hook's two -----------------------------------------------------------------


def test_a_model_call_adds_its_cost_under_its_role() -> None:
    """By role, because 'the run cost nine dollars' and 'the Coder cost eight of them' lead
    to different changes."""
    before = value("autoswe_cost_usd_total", role="coder")

    metrics.record_llm_call("coder", cost_usd=0.25, cache_hit_rate=0.5)

    assert value("autoswe_cost_usd_total", role="coder") == pytest.approx(before + 0.25)


def test_one_role_spending_does_not_move_another() -> None:
    before = value("autoswe_cost_usd_total", role="planner")

    metrics.record_llm_call("coder", cost_usd=1.0, cache_hit_rate=0.0)

    assert value("autoswe_cost_usd_total", role="planner") == pytest.approx(before)


def test_a_refunded_call_does_not_reduce_the_total() -> None:
    """A counter cannot go down; `prometheus_client` raises on a negative increment, and a
    provider correcting a charge must not be the thing that fails a run."""
    before = value("autoswe_cost_usd_total", role="review")

    metrics.record_llm_call("review", cost_usd=-5.0, cache_hit_rate=0.0)

    assert value("autoswe_cost_usd_total", role="review") == pytest.approx(before)


def test_every_call_is_counted_in_the_cache_histogram() -> None:
    before = value("autoswe_cache_hit_rate_count")

    metrics.record_llm_call("coder", cost_usd=0.0, cache_hit_rate=0.9)

    assert value("autoswe_cache_hit_rate_count") == before + 1


# ---- tasks ---------------------------------------------------------------------------------


def test_a_task_that_never_needed_debugging_counts_as_a_first_pass() -> None:
    before = value("autoswe_first_pass_test_success_total")

    metrics.record_task_done(debug_attempts=0)

    assert value("autoswe_first_pass_test_success_total") == before + 1


def test_a_task_that_needed_the_debugger_counts_separately() -> None:
    """Deliberately two counters rather than one with a label: a rise in debug successes
    with no rise in first passes is the debug loop covering for a worse Coder, and that is
    the shape worth seeing at a glance."""
    first, debug = (
        value("autoswe_first_pass_test_success_total"),
        value("autoswe_debug_success_total"),
    )

    metrics.record_task_done(debug_attempts=2)

    assert value("autoswe_debug_success_total") == debug + 1
    assert value("autoswe_first_pass_test_success_total") == first, "not both"


# ---- runs ------------------------------------------------------------------------------------


def test_a_finished_run_is_counted_under_its_outcome() -> None:
    before = value("autoswe_runs_total", outcome="done")

    metrics.record_run_finished(outcome="done", attempts=[0, 1], tasks_done=2, total_tokens=100)

    assert value("autoswe_runs_total", outcome="done") == before + 1


def test_every_task_contributes_an_attempt_count_including_the_easy_ones() -> None:
    """A histogram of only the tasks that struggled would say the agent always struggles."""
    before = value("autoswe_task_attempts_count")

    metrics.record_run_finished(outcome="done", attempts=[0, 0, 3], tasks_done=3, total_tokens=90)

    assert value("autoswe_task_attempts_count") == before + 3


def test_tokens_are_divided_by_the_tasks_that_finished() -> None:
    before_sum = value("autoswe_tokens_per_solved_task_sum")

    metrics.record_run_finished(outcome="done", attempts=[0], tasks_done=2, total_tokens=100_000)

    assert value("autoswe_tokens_per_solved_task_sum") == pytest.approx(before_sum + 50_000)


def test_a_run_that_finished_nothing_records_no_rate() -> None:
    """Dividing by no tasks is not a large number, it is not a number, and a zero would
    drag the average of the runs that did work."""
    before = value("autoswe_tokens_per_solved_task_count")

    metrics.record_run_finished(
        outcome="failed", attempts=[3, 3], tasks_done=0, total_tokens=500_000
    )

    assert value("autoswe_tokens_per_solved_task_count") == before
    assert value("autoswe_runs_total", outcome="failed") > 0, "the run itself is still counted"


def test_an_unpriced_run_records_no_rate_either() -> None:
    """A free or local model reports zero tokens through some gateways; dividing that is a
    measurement of nothing."""
    before = value("autoswe_tokens_per_solved_task_count")

    metrics.record_run_finished(outcome="done", attempts=[0], tasks_done=1, total_tokens=0)

    assert value("autoswe_tokens_per_solved_task_count") == before


# ---- the sandbox -------------------------------------------------------------------------


def test_a_command_records_how_long_it_took() -> None:
    before = value("autoswe_sandbox_exec_seconds_sum")

    metrics.record_sandbox_exec(2.5)

    assert value("autoswe_sandbox_exec_seconds_sum") == pytest.approx(before + 2.5)


# ---- nothing here may fail a run ----------------------------------------------------------


def test_a_metric_that_throws_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reason `_safe` exists. A number nobody is watching yet must not be able
    to fail a run that somebody is."""

    class Exploding:
        def labels(self, **_: Any) -> Any:
            raise RuntimeError("the registry is unhappy")

    monkeypatch.setattr(metrics, "COST", Exploding())

    metrics.record_llm_call("coder", cost_usd=1.0, cache_hit_rate=0.5)  # must not raise


def test_a_run_still_counts_when_its_attempt_counts_are_unusable() -> None:
    """Ordering inside `record_run_finished` is deliberate: the run is counted before the
    per-task loop, so a bad attempts iterable costs the histogram and not the run count."""

    def explodes_midway() -> Any:
        yield 0
        raise ValueError("a task with no id")

    before = value("autoswe_runs_total", outcome="cancelled")

    metrics.record_run_finished(
        outcome="cancelled", attempts=explodes_midway(), tasks_done=1, total_tokens=10
    )

    assert value("autoswe_runs_total", outcome="cancelled") == before + 1


# ---- exposition ----------------------------------------------------------------------------


def test_the_endpoint_body_names_the_metrics_it_carries() -> None:
    metrics.record_llm_call("coder", cost_usd=0.01, cache_hit_rate=0.5)

    body, content_type = metrics.render()

    assert b"autoswe_cost_usd_total" in body
    assert b"autoswe_cache_hit_rate_bucket" in body
    assert content_type.startswith("text/plain")


def test_the_body_carries_no_repository_or_goal() -> None:
    """The endpoint is unauthenticated, like `/healthz`. That is only defensible while
    every label is a role or an outcome — never a repository, a goal, or a customer."""
    metrics.record_run_finished(outcome="done", attempts=[0], tasks_done=1, total_tokens=10)

    body, _ = metrics.render()

    label_names = {
        part.split("=")[0]
        for line in body.decode().splitlines()
        if line.startswith("autoswe_") and "{" in line
        for part in line.split("{", 1)[1].split("}")[0].split(",")
    }
    assert label_names <= {"role", "outcome", "le", "quantile"}, sorted(label_names)


# ---- the worker's endpoint ------------------------------------------------------------------


def test_the_worker_endpoint_is_optional() -> None:
    """A second worker on the same host cannot have the port, and a developer running one
    on a laptop does not want it."""
    from orchestrator.worker import start_metrics_server

    start_metrics_server(0)  # must not raise, must not bind


def test_a_taken_port_costs_the_metrics_and_not_the_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The normal failure: a second worker starts on the same host. Losing the metrics is
    the right answer; refusing the runs is not."""
    import orchestrator.worker as worker

    def refuse(port: int) -> None:
        raise OSError("address already in use")

    monkeypatch.setattr("prometheus_client.start_http_server", refuse)

    worker.start_metrics_server(9100)  # must not raise
