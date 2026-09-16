"""gitleaks: committed secrets. Every finding is critical, and none of them keeps its value.

Two rules here are not negotiable.

**Severity is always critical.** gitleaks has no severity of its own, and a committed
secret is not a matter of degree: it is in the history, and the remedy is rotation whatever
the rule was called.

**The secret never leaves this function.** The finding carries the rule, the file and the
line, and `Secret` and `Match` are dropped on the floor. They would otherwise travel into
a report, an artifact, a log line, and — worst of all — a pull request body on GitHub,
which is publishing the thing the scanner exists to catch.
"""

from __future__ import annotations

from typing import Any

from contracts import SecurityFinding


def parse_gitleaks(data: list[dict[str, Any]]) -> list[SecurityFinding]:
    """gitleaks' JSON report: a flat list, one entry per leak."""
    out = []
    for f in data:
        rule = str(f.get("RuleID") or "?")
        commit = str(f.get("Commit") or "")[:8]
        where = f" in commit {commit}" if commit else ""
        out.append(
            SecurityFinding(
                tool="gitleaks",
                rule=rule,
                file=str(f.get("File") or ""),
                line=int(f.get("StartLine") or 0),
                # Not a mapping: there is one severity a committed secret can have.
                severity="critical",
                # `Description` is gitleaks' own wording for the rule. The matched text is
                # deliberately absent — see the module docstring.
                message=f"{f.get('Description') or rule}{where} (value withheld)"[:500],
                verified_by_llm=False,
                false_positive=False,
                rationale="",
                in_diff=False,
            )
        )
    return out
