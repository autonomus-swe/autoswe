"""bandit: Python static analysis. Reports a file and a line, and its own severity names."""

from __future__ import annotations

from typing import Any

from contracts import SecurityFinding, SecuritySeverity

# bandit's own three levels. It has no notion of `critical`: the highest thing it will say
# is HIGH, and promoting that to critical here would put a gate in a severity map.
SEVERITY: dict[str, SecuritySeverity] = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}


def parse_bandit(data: dict[str, Any]) -> list[SecurityFinding]:
    """``results`` from ``bandit -f json``, plus its ``errors`` as findings of their own.

    The errors matter. bandit reports a file it could not parse in `errors` rather than
    failing, and a file nobody scanned is not a file with nothing wrong in it.
    """
    out = []
    for r in data.get("results", []):
        out.append(
            SecurityFinding(
                tool="bandit",
                rule=str(r.get("test_id") or "?"),
                file=str(r.get("filename") or ""),
                line=int(r.get("line_number") or 0),
                severity=SEVERITY.get(str(r.get("issue_severity", "")).upper(), "medium"),
                message=str(r.get("issue_text") or "")[:500],
                verified_by_llm=False,
                false_positive=False,
                rationale="",
                in_diff=False,
            )
        )
    for err in data.get("errors", []):
        out.append(
            SecurityFinding(
                tool="bandit",
                rule="scan-error",
                file=str(err.get("filename") or ""),
                line=0,
                severity="info",
                message=f"bandit could not scan this file: {err.get('reason', '')}"[:500],
                verified_by_llm=False,
                false_positive=False,
                rationale="",
                in_diff=False,
            )
        )
    return out
