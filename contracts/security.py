from __future__ import annotations

from typing import Literal

from pydantic import Field

from contracts.common import LLMModel

SecuritySeverity = Literal["critical", "high", "medium", "low", "info"]


class SecurityFinding(LLMModel):
    tool: str
    rule: str
    file: str
    line: int = Field(ge=0)
    severity: SecuritySeverity
    message: str
    verified_by_llm: bool
    false_positive: bool
    rationale: str


class SecurityChecklist(LLMModel):
    """Fixed-key checklist (README §9). Used instead of an open dict if the API rejects maps."""

    no_secrets: bool
    inputs_validated: bool
    parameterized_sql: bool
    xss_escaped: bool
    csrf_protected: bool
    auth_on_endpoints: bool
    rate_limited_auth: bool
    no_stack_traces: bool
    env_validated: bool
    no_pii_in_cache: bool
    no_pii_in_logs: bool


class SecurityReport(LLMModel):
    findings: list[SecurityFinding]
    critical: bool
    checklist: dict[str, bool]
