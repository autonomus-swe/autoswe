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
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )
