"""Parse pytest-json-report output into a :class:`TestReport` (v1: counts + messages)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from contracts import FailureKind, TestFailure, TestReport

_KINDS: list[tuple[re.Pattern[str], FailureKind]] = [
    (re.compile(r"ImportError|ModuleNotFoundError|No module named"), "import"),
    (re.compile(r"Timeout|timed out", re.I), "timeout"),
    (re.compile(r"AssertionError|^assert\b|\bassert ", re.M), "assertion"),
]


def guess_kind(message: str) -> FailureKind:
    for pattern, kind in _KINDS:
        if pattern.search(message):
            return kind
    return "exception"


def signature(test_id: str, kind: str) -> str:
    return hashlib.sha1(f"{test_id}|{kind}".encode(), usedforsecurity=False).hexdigest()


def _first_line(longrepr: Any) -> str:
    text = longrepr if isinstance(longrepr, str) else str(longrepr or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # pytest puts the summary ("E   AssertionError: ...") near the end; prefer it
    for ln in reversed(lines):
        if ln.startswith("E "):
            return ln[1:].strip()[:300]
    return (lines[-1] if lines else "unknown failure")[:300]


def failure(test_id: str, message: str, kind: FailureKind | None = None) -> TestFailure:
    k = kind or guess_kind(message)
    return TestFailure(
        test_id=test_id, kind=k, message=message, frames=[], signature=signature(test_id, k)
    )


def environment_report(command: str, message: str, output: str = "") -> TestReport:
    return TestReport(
        passed=False,
        total=0,
        failed=0,
        errors=1,
        skipped=0,
        failures=[failure("<environment>", message, "environment")],
        duration_s=0.0,
        command=command,
        truncated_output=output[-4000:],
    )


def parse_json_report(data: dict[str, Any], command: str, output: str = "") -> TestReport:
    summary = data.get("summary", {})
    failures: list[TestFailure] = []
    for collector in data.get("collectors", []):
        if collector.get("outcome") == "failed":
            msg = _first_line(collector.get("longrepr"))
            failures.append(failure(collector.get("nodeid") or "<collection>", msg))
    for test in data.get("tests", []):
        if test.get("outcome") not in ("failed", "error"):
            continue
        phase = test.get("call") or test.get("setup") or test.get("teardown") or {}
        failures.append(failure(test.get("nodeid", "?"), _first_line(phase.get("longrepr"))))
    total = int(summary.get("total", 0))
    failed = int(summary.get("failed", 0))
    collector_failures = sum(1 for c in data.get("collectors", []) if c.get("outcome") == "failed")
    errors = max(int(summary.get("error", 0)), collector_failures)
    if total == 0 and not failures:
        failures.append(failure("<environment>", "no tests were collected", "environment"))
        errors = max(errors, 1)
    return TestReport(
        passed=total > 0 and failed == 0 and errors == 0,
        total=total,
        failed=failed,
        errors=errors,
        skipped=int(summary.get("skipped", 0)),
        failures=failures,
        duration_s=float(data.get("duration", 0.0)),
        command=command,
        truncated_output=output[-4000:],
    )


def summarize(report: TestReport, max_failures: int = 20) -> str:
    passed = report.total - report.failed - report.errors - report.skipped
    head = (
        f"{max(passed, 0)} passed, {report.failed} failed, {report.errors} errors, "
        f"{report.skipped} skipped in {report.duration_s:.2f}s"
        + (" — ALL TESTS PASS" if report.passed else "")
    )
    lines = [head]
    for f in report.failures[:max_failures]:
        lines.append(f"- {f.test_id} [{f.kind}]: {f.message}")
    if len(report.failures) > max_failures:
        lines.append(f"… and {len(report.failures) - max_failures} more")
    return "\n".join(lines)
