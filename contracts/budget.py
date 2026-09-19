from __future__ import annotations

from pydantic import Field

from contracts.common import StateModel


class Budget(StateModel):
    """Per-run limits. Exceeding one is a normal transition (ESCALATE), not an exception."""

    max_debug_attempts: int = Field(default=3, ge=0)
    max_fix_rounds: int = Field(default=2, ge=0)
    wall_clock_s: int = Field(default=45 * 60, gt=0)
    max_usd: float = Field(default=10.0, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    warn_at_fraction: float = Field(default=0.9, gt=0, le=1)

    def fraction_used(
        self, usage: Usage, elapsed_s: float, *, cost_measurable: bool = True
    ) -> dict[str, float]:
        """How much of each limit is gone, keyed by the reason it would produce.

        Wall clock is always included because it is the one dimension that is always
        measurable: a free or unpriced model spends no dollars and may have no token
        limit, so without it such a run would have no bound at all.

        ``cost_measurable=False`` drops the dollar dimension rather than reporting a
        reassuring zero. A model we have no price for has not cost nothing — we simply
        cannot say, and a budget that silently reads 0 % is worse than one that admits
        it does not apply.
        """
        fractions = {"budget_wall_clock": elapsed_s / self.wall_clock_s}
        if cost_measurable:
            fractions["budget_usd"] = usage.cost_usd / self.max_usd
        if self.max_tokens:
            fractions["budget_tokens"] = usage.total_tokens / self.max_tokens
        return fractions

    def exceeded(self, usage: Usage, elapsed_s: float, *, cost_measurable: bool = True) -> bool:
        return (
            max(self.fraction_used(usage, elapsed_s, cost_measurable=cost_measurable).values())
            >= 1.0
        )

    def reason(self, usage: Usage, elapsed_s: float, *, cost_measurable: bool = True) -> str:
        """Which limit ran out. The largest fraction wins, so the answer is the real cause."""
        fractions = self.fraction_used(usage, elapsed_s, cost_measurable=cost_measurable)
        return max(fractions, key=lambda k: fractions[k])

    def crossed_warning(
        self, usage: Usage, elapsed_s: float, *, cost_measurable: bool = True
    ) -> list[str]:
        """Limits past the warning line but not yet spent, for a once-only warning."""
        fractions = self.fraction_used(usage, elapsed_s, cost_measurable=cost_measurable)
        return sorted(k for k, v in fractions.items() if self.warn_at_fraction <= v < 1.0)


class Usage(StateModel):
    """What has been spent so far. The source of truth is the ``llm_calls`` table."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    wall_clock_s: float = 0.0

    def add(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            wall_clock_s=self.wall_clock_s + other.wall_clock_s,
        )

    @property
    def cache_hit_rate(self) -> float:
        """How much of what the model read came from cache.

        Against `input + cache_read` rather than `total_tokens`: output tokens were never
        candidates for a cache hit, so counting them would make a long answer look like a
        caching failure. Cache *writes* are excluded for the same reason from the
        denominator — a write is the price of the next call's read, and it is charged
        separately.
        """
        read = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / read if read else 0.0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )
