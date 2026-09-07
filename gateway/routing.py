"""Role -> model tier + effort (README §4.7). Providers map tiers to concrete models: the
Anthropic provider uses ``ANTHROPIC_MODELS``; OpenAI-compatible providers use the single
configured model for every tier and ignore effort."""

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


def route_for(role: str) -> Route:
    return ROUTES[role]
