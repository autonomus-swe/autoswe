"""semgrep: rule-pack matching. Nests the interesting fields under ``extra``."""

from __future__ import annotations

from typing import Any

from contracts import SecurityFinding, SecuritySeverity

# semgrep's INFO is advisory rather than low-severity, so it maps to `info` and cannot
# contribute to a gate.
SEVERITY: dict[str, SecuritySeverity] = {"ERROR": "high", "WARNING": "medium", "INFO": "info"}


def parse_semgrep(data: dict[str, Any]) -> list[SecurityFinding]:
    """``results`` from ``semgrep --json``, and its ``errors`` as `info` findings.

    A semgrep error is usually a rule that could not run or a file it could not parse. The
    run is not wrong, but the coverage is smaller than it looks, so it is reported.
    """
    out = []
    for r in data.get("results", []):
        extra = r.get("extra") or {}
        out.append(
            SecurityFinding(
                tool="semgrep",
                rule=str(r.get("check_id") or "?"),
                file=str(r.get("path") or ""),
                line=int((r.get("start") or {}).get("line") or 0),
                severity=SEVERITY.get(str(extra.get("severity", "")).upper(), "medium"),
                message=str(extra.get("message") or "")[:500],
                verified_by_llm=False,
                false_positive=False,
                rationale="",
                in_diff=False,
            )
        )
    for err in data.get("errors", []):
        out.append(
            SecurityFinding(
                tool="semgrep",
                rule="scan-error",
                file=str(err.get("path") or ""),
                line=0,
                severity="info",
                message=f"semgrep error: {err.get('message') or err.get('type') or ''}"[:500],
                verified_by_llm=False,
                false_positive=False,
                rationale="",
                in_diff=False,
            )
        )
    return out
