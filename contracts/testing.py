from __future__ import annotations

from typing import Literal

from pydantic import Field

from contracts.common import LLMModel

FailureKind = Literal["assertion", "exception", "import", "environment", "timeout"]


class Frame(LLMModel):
    file: str
    line: int
    function: str
    code: str
    in_repo: bool


class TestFailure(LLMModel):
    test_id: str
    kind: FailureKind
    message: str
    frames: list[Frame]
    signature: str


class TestReport(LLMModel):
    passed: bool
    total: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failures: list[TestFailure]
    duration_s: float = Field(ge=0)
    command: str
    truncated_output: str
    # one signature for the whole report, so "same failure as last time" is a
    # single comparison in the transition table
    signature: str = ""
