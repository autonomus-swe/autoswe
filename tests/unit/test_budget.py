"""Budget arithmetic, and what happens when spend cannot be measured at all.

Budgets decide when a run escalates instead of grinding, so the interesting cases are the
boundaries and the dimension that quietly does not apply.
"""

from __future__ import annotations

import pytest

from contracts import Budget, Usage
from gateway import pricing

pytestmark = pytest.mark.unit

BUDGET = Budget(wall_clock_s=1000, max_usd=10.0, max_tokens=100_000)


@pytest.mark.parametrize(
    ("usage", "elapsed", "expected"),
    [
        (Usage(), 0, {"budget_wall_clock": 0.0, "budget_usd": 0.0, "budget_tokens": 0.0}),
        (
            Usage(cost_usd=5.0, input_tokens=50_000),
            500,
            {"budget_wall_clock": 0.5, "budget_usd": 0.5, "budget_tokens": 0.5},
        ),
        (
            Usage(cost_usd=10.0, input_tokens=100_000),
            1000,
            {"budget_wall_clock": 1.0, "budget_usd": 1.0, "budget_tokens": 1.0},
        ),
        # tokens count every kind, not just the prompt
        (
            Usage(input_tokens=40_000, output_tokens=10_000, cache_read_tokens=10_000),
            0,
            {"budget_wall_clock": 0.0, "budget_usd": 0.0, "budget_tokens": 0.6},
        ),
    ],
)
def test_fraction_used(usage: Usage, elapsed: float, expected: dict[str, float]) -> None:
    assert BUDGET.fraction_used(usage, elapsed) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("usage", "elapsed", "exceeded", "reason"),
    [
        # not a tie: 99.9 % of the dollars against 10 % of the clock
        (Usage(cost_usd=9.99), 100, False, "budget_usd"),
        (Usage(cost_usd=10.0), 1, True, "budget_usd"),
        (Usage(cost_usd=1.0), 1000, True, "budget_wall_clock"),
        (Usage(input_tokens=100_000), 1, True, "budget_tokens"),
        # the largest fraction is the real cause, even when several are over
        (Usage(cost_usd=30.0), 1001, True, "budget_usd"),
    ],
)
def test_exceeded_and_reason(usage: Usage, elapsed: float, exceeded: bool, reason: str) -> None:
    assert BUDGET.exceeded(usage, elapsed) is exceeded
    assert BUDGET.reason(usage, elapsed) == reason


def test_a_limit_is_spent_at_exactly_one_hundred_percent() -> None:
    """The boundary is inclusive: 100 % is spent, not nearly spent."""
    assert not BUDGET.exceeded(Usage(cost_usd=9.9999), 0)
    assert BUDGET.exceeded(Usage(cost_usd=10.0), 0)


@pytest.mark.parametrize(
    ("usage", "elapsed", "warned"),
    [
        (Usage(cost_usd=8.9), 0, []),
        (Usage(cost_usd=9.0), 0, ["budget_usd"]),
        (Usage(cost_usd=9.5), 950, ["budget_usd", "budget_wall_clock"]),
        # already spent is not a warning; it is an escalation
        (Usage(cost_usd=10.0), 0, []),
    ],
)
def test_warning_line(usage: Usage, elapsed: float, warned: list[str]) -> None:
    assert BUDGET.crossed_warning(usage, elapsed) == warned


def test_wall_clock_is_the_only_bound_a_free_run_has() -> None:
    """A free model spends no dollars, and by default there is no token limit.

    Without wall clock in the mix such a run would have no budget at all, which is how a
    loop spins forever on a local model that costs nothing.
    """
    free = Budget()  # defaults: no max_tokens
    fractions = free.fraction_used(Usage(input_tokens=10_000_000), 10, cost_measurable=False)
    assert list(fractions) == ["budget_wall_clock"]
    assert free.exceeded(Usage(), free.wall_clock_s, cost_measurable=False)
    assert free.reason(Usage(), free.wall_clock_s, cost_measurable=False) == "budget_wall_clock"


def test_an_unpriced_model_drops_the_dollar_budget_rather_than_reporting_zero() -> None:
    """We have not measured $0; we cannot measure it. Those are different claims."""
    spent = Usage(input_tokens=5_000_000)
    with_cost = BUDGET.fraction_used(spent, 10)
    without = BUDGET.fraction_used(spent, 10, cost_measurable=False)
    assert with_cost["budget_usd"] == 0.0  # reassuring and meaningless
    assert "budget_usd" not in without
    # the token budget still bites, so the run is not unbounded
    assert BUDGET.exceeded(spent, 10, cost_measurable=False)
    assert BUDGET.reason(spent, 10, cost_measurable=False) == "budget_tokens"


# ---- pricing, which is what makes a dollar budget mean anything --------------------


@pytest.mark.parametrize(
    ("model", "base_url", "measurable"),
    [
        # vendors ship dated ids; the price is keyed on the family
        ("claude-haiku-4-5-20251001", "https://api.anthropic.com/v1/", True),
        ("claude-opus-5", None, True),
        ("openrouter/free", "https://openrouter.ai/api/v1", True),
        ("poolside/laguna-s-2.1:free", "https://openrouter.ai/api/v1", True),
        # a bare ollama tag says nothing about where it runs, so the endpoint decides
        ("qwen2.5:7b", "http://localhost:11434/v1", True),
        ("qwen2.5:7b", "https://some-paid-host/v1", False),
        ("who/knows", "https://example.com/v1", False),
    ],
)
def test_priced_models(model: str, base_url: str | None, measurable: bool) -> None:
    assert pricing.priced(model, base_url) is measurable


def test_a_dated_claude_id_costs_what_its_family_costs() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    dated = pricing.cost("claude-haiku-4-5-20251001", usage)
    family = pricing.cost("claude-haiku-4-5", usage)
    assert dated == family == pytest.approx(1.0 + 5.0)


def test_free_and_unpriced_both_cost_zero_but_are_distinguishable() -> None:
    """The number is the same; only `priced` tells you whether to trust it."""
    usage = Usage(input_tokens=1_000_000)
    assert pricing.cost("openrouter/free", usage) == 0.0
    assert pricing.cost("who/knows", usage) == 0.0
    assert pricing.priced("openrouter/free") and not pricing.priced("who/knows")


def test_the_longest_matching_family_wins() -> None:
    """So a future `claude-opus-5-mini` cannot be priced as `claude-opus-5`."""
    assert pricing.price_for("claude-opus-5-20260101") == pricing.PRICES["claude-opus-5"]
