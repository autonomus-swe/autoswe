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


class ReviewRejection(LLMModel):
    """A candidate the verification pass threw out, and why.

    This field exists because the prompt already demanded it and the contract had nowhere
    to put it: `review.md` tells the model "reject it with a one-line reason. Rejections
    are recorded and read", while `ReviewReport` carried only the confirmed findings. So
    every rejection was implicit — a candidate simply absent from the output — and the
    `review` artifact stored an empty string where the reason should have been.

    Kept separate from `ReviewFinding` rather than adding a `rejected` flag to it, because
    a rejection is not a finding with a different status: it has no severity, it cannot
    gate, and the only thing worth keeping about it is the sentence explaining it.
    """

    file: str
    line: int = Field(ge=0)
    reason: str


class ReviewReport(LLMModel):
    findings: list[ReviewFinding]
    blocking: bool
    # Defaulted, so a model that returns only findings still validates — the schema this
    # generates lists `rejections` as optional. A silently dropped candidate is then
    # visible in the artifact as exactly that, rather than as a rejection with a blank
    # reason, which read the same and meant something different.
    rejections: list[ReviewRejection] = Field(default_factory=list)
