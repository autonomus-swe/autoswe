from __future__ import annotations

from typing import Literal

from pydantic import Field

from contracts.common import LLMModel
from contracts.testing import FailureKind


class DebugHypothesis(LLMModel):
    failure_class: FailureKind | Literal["flaky"]
    root_cause: str
    plan: str
    confidence: float = Field(ge=0, le=1)
