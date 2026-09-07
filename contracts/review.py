from __future__ import annotations

from typing import Literal

from pydantic import Field

from contracts.common import LLMModel

Severity = Literal["blocking", "major", "minor", "nit"]


class ReviewFinding(LLMModel):
    file: str
    line: int = Field(ge=0)
    severity: Severity
    category: str
    summary: str
    failure_scenario: str


class ReviewCandidates(LLMModel):
    """Output of the cheap pre-pass."""

    findings: list[ReviewFinding]


class ReviewReport(LLMModel):
    findings: list[ReviewFinding]
    blocking: bool
