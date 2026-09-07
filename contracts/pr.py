from __future__ import annotations

from pydantic import Field

from contracts.common import LLMModel


class PullRequestDescription(LLMModel):
    title: str = Field(max_length=120)
    summary: str
    changes: list[str]
    testing: str
    known_issues: list[str]
    rollback: str
