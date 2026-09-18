"""bandit: Python static analysis. Reports a file and a line, and its own severity names."""

from __future__ import annotations

from typing import Any

from contracts import SecurityFinding, SecuritySeverity

# bandit's own three levels. It has no notion of `critical`: the highest thing it will say
# is HIGH, and promoting that to critical here would put a gate in a severity map.
SEVERITY: dict[str, SecuritySeverity] = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}

# The hardcoded-credential family, whose `issue_text` **quotes the value it found**:
# `Possible hardcoded password: 'hunter2'`. Verified against bandit 1.8.6.
#
# The value has to be withheld here for the same reason gitleaks is run with `--redact`:
# a finding's `message` is rendered into the pull request body, which is published to a
# forge and copied into notification email. gitleaks redacts its own and semgrep's rule
# messages are static templates, so this was the one path by which a credential could be
# published — and it is the path that stays open when gitleaks *misses*, which it does for
# anything below its entropy threshold. `DB_PASSWORD = "hunter2"` is flagged by B105 and
# by nothing else, so the push is not refused and the body would have carried it.
#
# A rule id set rather than a regex over the message: which rules quote a literal is a
# fact about bandit, and matching on the prose would break the first time they reword it.
SECRET_RULES = frozenset({"B105", "B106", "B107"})
WITHHELD = "a credential appears to be hardcoded here (value withheld)"


def parse_bandit(data: dict[str, Any]) -> list[SecurityFinding]:
    """``results`` from ``bandit -f json``, plus its ``errors`` as findings of their own.

    The errors matter. bandit reports a file it could not parse in `errors` rather than
    failing, and a file nobody scanned is not a file with nothing wrong in it.
    """
    out = []
    for r in data.get("results", []):
        rule = str(r.get("test_id") or "?")
        if rule in SECRET_RULES:
            # The rule name, not the quoted value. Keeping the name means the finding is
            # still actionable: the file and the line say where, and the rule says what.
            message = f"{r.get('test_name') or rule}: {WITHHELD}"
        else:
            message = str(r.get("issue_text") or "")[:500]
        out.append(
            SecurityFinding(
                tool="bandit",
                rule=rule,
                file=str(r.get("filename") or ""),
                line=int(r.get("line_number") or 0),
                severity=SEVERITY.get(str(r.get("issue_severity", "")).upper(), "medium"),
                message=message,
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
