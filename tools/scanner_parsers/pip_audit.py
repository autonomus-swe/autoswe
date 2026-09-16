"""pip-audit: known vulnerabilities in the dependency set.

Two things about its real output that a hand-written fixture would not have shown.

**It reports every vulnerability twice.** Running it on `requests==2.19.0` returns ten
entries for five distinct ids — it resolves through more than one advisory source and does
not deduplicate. Parsed naively, a report would double every count it prints.

**There is no severity.** Its JSON carries `id`, `fix_versions`, `aliases` and a
description, and nothing else. So the phase document's "map by CVSS if present, else high"
is in practice always `high`, and pretending otherwise would be inventing a number.
"""

from __future__ import annotations

from typing import Any

from contracts import SecurityFinding

# A dependency file has no line to point at, and a vulnerability is a property of the
# pinned version rather than of a place in a file.
MANIFEST = "(dependencies)"


def parse_pip_audit(data: dict[str, Any]) -> list[SecurityFinding]:
    """``dependencies`` from ``pip-audit -f json``, deduplicated by (package, id)."""
    out: list[SecurityFinding] = []
    seen: set[tuple[str, str]] = set()
    for dep in data.get("dependencies", []):
        name = str(dep.get("name") or "?")
        version = str(dep.get("version") or "?")
        for vuln in dep.get("vulns", []) or []:
            vuln_id = str(vuln.get("id") or "?")
            if (name, vuln_id) in seen:
                continue  # pip-audit lists the same advisory once per source
            seen.add((name, vuln_id))
            fixes = ", ".join(str(v) for v in (vuln.get("fix_versions") or [])) or "none published"
            out.append(
                SecurityFinding(
                    tool="pip-audit",
                    rule=vuln_id,
                    file=MANIFEST,
                    line=0,
                    # Not read from the data: the data has no severity. See the docstring.
                    severity="high",
                    message=(
                        f"{name} {version} is affected by {vuln_id} "
                        f"(fixed in: {fixes}). {vuln.get('description') or ''}"
                    )[:500],
                    verified_by_llm=False,
                    false_positive=False,
                    rationale="",
                    in_diff=False,
                )
            )
    return out
