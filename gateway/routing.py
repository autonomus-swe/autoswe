"""Role -> model tier + effort (README §4.7), and the one condition that changes it.

A tier is not a model. The Anthropic provider maps tiers through `ANTHROPIC_MODELS`; an
OpenAI-compatible provider maps them through whatever `LLM_MODEL_OPUS` / `_SONNET` /
`_HAIKU` are set to, and falls back to its single configured model for every tier when
they are not. That fallback is why `model_for` exists on the provider rather than here:
only the provider knows which model ids its endpoint will accept.

## Downgrading near the budget

Past 90 % of the *dollar* budget, three roles drop a tier. Which three is the whole
decision, and it is a claim about where a cheaper model costs least:

- **planner, decomposer, review** downgrade. Each produces a structured document in one
  or two calls. A weaker model writes a less elegant plan; the work still happens.
- **coder and debugger never downgrade.** A cheaper Coder that needs three attempts is
  not cheaper — it is the same money spent on three times the tool calls, plus a debug
  loop that would not otherwise have run (README §4.7).
- **tester, pr_writer, review_pre, security** are already on the floor.

Only dollars trigger it. A run near its wall clock is not helped by a cheaper model,
which is not a faster one and on a hard task is slower.

## A downgrade is a new cache namespace

Changing the model for a role starts that role's prompt cache over: the cached prefix is
keyed per model, so the first call after a downgrade writes rather than reads. Accepted —
the saving from a cheaper tier is far larger than one cache write — but logged, because a
cache hit rate that falls off a cliff mid-run otherwise looks like the caching broke.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["opus", "sonnet", "haiku"]
Effort = Literal["low", "medium", "high", "xhigh"]


@dataclass(frozen=True)
class Route:
    tier: Tier
    effort: Effort


ROUTES: dict[str, Route] = {
    "analyzer": Route("sonnet", "medium"),
    "planner": Route("opus", "high"),
    "decomposer": Route("opus", "high"),
    "coder": Route("opus", "xhigh"),
    "tester": Route("haiku", "low"),
    "debugger": Route("opus", "xhigh"),
    "review_pre": Route("sonnet", "medium"),
    "review": Route("opus", "high"),
    "security": Route("sonnet", "medium"),
    "pr_writer": Route("sonnet", "low"),
}

ANTHROPIC_MODELS: dict[Tier, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

# The roles that give up a tier when the money runs low, and what they drop to.
#
# Keyed by the names in `ROUTES` — the phase document writes `"decompose"`, which is not a
# role here, so that entry would never have matched and the decomposer would have stayed on
# opus while the code looked like it downgraded.
DOWNGRADE: dict[str, Tier] = {
    "planner": "sonnet",
    "decomposer": "sonnet",
    "review": "sonnet",
}


def route_for(role: str, *, downgrade: bool = False) -> Route:
    """The route for a role, one tier lower if the run is short of money and this role
    is one of the three that gives one up.

    `downgrade` is a boolean the caller computes rather than the run state the phase
    document passes: the gateway does not get to see the state. Everything in here would
    then be able to read the phase, the plan and the task list, and a module that can read
    all of that ends up steering the run — which is the phase machine's job. The same rule
    keeps `BudgetGate` narrow.
    """
    base = ROUTES[role]
    if downgrade and role in DOWNGRADE:
        return Route(tier=DOWNGRADE[role], effort=base.effort)
    return base
