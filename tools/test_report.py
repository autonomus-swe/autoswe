"""Parse a test run into a :class:`TestReport` with stack frames and stable signatures.

v2. The Debugger is only as good as its evidence, so a failure carries the frames that
produced it, the source line at each, and an exception type — parsed here, host-side,
from what the runner wrote. Nothing in this module trusts the model.

The signature deliberately excludes line numbers. A Debugger edits code and lines move;
"the same failure" means the same test failing the same way in the same function, which is
what lets the transition table notice that an attempt changed nothing.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from contracts import FailureKind, Frame, TestFailure, TestReport
from repo import source_context as src

MAX_FRAMES = 12
MESSAGE_CHARS = 300

# First match wins, so order is the classification policy. Import errors are checked
# before assertions because a collection failure often mentions both.
_KINDS: list[tuple[re.Pattern[str], FailureKind]] = [
    (re.compile(r"ModuleNotFoundError|ImportError|No module named|cannot import name"), "import"),
    (re.compile(r"\bTimeout\b|timed out|Failed: Timeout", re.I), "timeout"),
    (
        re.compile(
            r"ConnectionError|ConnectionRefusedError|PermissionError|FileNotFoundError"
            r"|OSError|socket\.gaierror|Name or service not known|No such file or directory"
        ),
        "environment",
    ),
    (re.compile(r"AssertionError|^E\s+assert\b|\bassert ", re.M), "assertion"),
]

# "  /path/to/file.py:42: in function_name" — pytest's long representation.
_LONGREPR_FRAME = re.compile(r"^(?P<file>[^\s:][^:]*):(?P<line>\d+): in (?P<func>\S+)\s*$", re.M)
# "E   ValueError: message" or "ValueError: message"
# `socket.gaierror` is as much an exception type as `ValueError`, so the suffix match
# is case-insensitive; requiring the suffix at all is what keeps prose out.
_EXC_TYPE = re.compile(
    r"^(?:E\s+)?(?P<exc>[A-Za-z_][A-Za-z0-9_.]*(?:[Ee]rror|Exception|Warning))\b"
)


def guess_kind(message: str) -> FailureKind:
    for pattern, kind in _KINDS:
        if pattern.search(message):
            return kind
    return "exception"


def exc_type_of(message: str, longrepr: str = "") -> str:
    """The exception class name, for the signature. "" when nothing looks like one."""
    for text in (message, longrepr):
        for line in text.splitlines():
            m = _EXC_TYPE.match(line.strip())
            if m:
                return m.group("exc")
    return ""


def signature(
    test_id: str, kind: str, exc_type: str = "", frames: list[Frame] | None = None
) -> str:
    """Stable across edits: the test, how it failed, and where — never which line.

    Keyed on the topmost frame *in the repository*, because the bottom of a traceback is
    usually library code that is the same for every caller.
    """
    top = next((f for f in (frames or []) if f.in_repo), None)
    key = f"{test_id}|{kind}|{exc_type}|{top.file if top else ''}|{top.function if top else ''}"
    return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]


def report_signature(failures: list[TestFailure]) -> str:
    """One signature for a whole report, so "same failure as last time" is one comparison."""
    joined = "|".join(sorted(f.signature for f in failures))
    return hashlib.sha1(joined.encode(), usedforsecurity=False).hexdigest()[:16]


def _text(longrepr: Any) -> str:
    if isinstance(longrepr, str):
        return longrepr
    if isinstance(longrepr, dict):  # json-report can nest it under reprcrash/reprtraceback
        crash = longrepr.get("reprcrash") or {}
        return str(crash.get("message") or longrepr.get("longrepr") or longrepr)
    return str(longrepr or "")


def _first_line(longrepr: Any) -> str:
    text = _text(longrepr)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # pytest puts the summary ("E   AssertionError: ...") near the end; prefer it
    for ln in reversed(lines):
        if ln.startswith("E "):
            return ln[1:].strip()[:MESSAGE_CHARS]
    return (lines[-1] if lines else "unknown failure")[:MESSAGE_CHARS]


def _functions_by_location(longrepr: str) -> dict[tuple[str, int], str]:
    """``(file, line) -> function`` from pytest's long representation.

    The json report's traceback entries carry a path and a line but no function name; the
    long representation carries all three, so it is the only place to recover it.
    """
    out: dict[tuple[str, int], str] = {}
    for m in _LONGREPR_FRAME.finditer(longrepr):
        out[(m.group("file"), int(m.group("line")))] = m.group("func")
    return out


def frames_from(phase: dict[str, Any], worktree: Path | None) -> list[Frame]:
    """Build frames from a json-report phase (``call``, ``setup`` or ``teardown``)."""
    longrepr = _text(phase.get("longrepr"))
    functions = _functions_by_location(longrepr)
    entries = list(phase.get("traceback") or [])
    crash = phase.get("crash") or {}
    # A crash entry is the innermost frame and is sometimes the only one reported.
    if crash.get("path") and not any(
        e.get("path") == crash.get("path") and e.get("lineno") == crash.get("lineno")
        for e in entries
    ):
        entries.append(crash)

    frames: list[Frame] = []
    for entry in entries[:MAX_FRAMES]:
        path = str(entry.get("path") or "")
        if not path:
            continue
        try:
            line = int(entry.get("lineno") or 0)
        except (TypeError, ValueError):
            line = 0
        inside = src.in_repo(path, worktree) if worktree else False
        frames.append(
            Frame(
                file=src.relative(path, worktree) if worktree else path,
                line=line,
                function=functions.get((path, line), "?"),
                code=src.line_at(path, line, worktree) if worktree and inside else "",
                in_repo=inside,
            )
        )
    return frames


def failure(
    test_id: str,
    message: str,
    kind: FailureKind | None = None,
    frames: list[Frame] | None = None,
    longrepr: str = "",
) -> TestFailure:
    k = kind or guess_kind(f"{message}\n{longrepr}")
    exc = exc_type_of(message, longrepr)
    fr = frames or []
    return TestFailure(
        test_id=test_id,
        kind=k,
        message=message,
        frames=fr,
        signature=signature(test_id, k, exc, fr),
    )


def environment_report(command: str, message: str, output: str = "") -> TestReport:
    fail = failure("<environment>", message, "environment")
    return TestReport(
        passed=False,
        total=0,
        failed=0,
        errors=1,
        skipped=0,
        failures=[fail],
        duration_s=0.0,
        command=command,
        truncated_output=output[-4000:],
        signature=report_signature([fail]),
    )


def parse_json_report(
    data: dict[str, Any], command: str, output: str = "", worktree: Path | None = None
) -> TestReport:
    summary = data.get("summary", {})
    failures: list[TestFailure] = []
    for collector in data.get("collectors", []):
        if collector.get("outcome") == "failed":
            longrepr = _text(collector.get("longrepr"))
            failures.append(
                failure(
                    collector.get("nodeid") or "<collection>",
                    _first_line(collector.get("longrepr")),
                    frames=frames_from(collector, worktree),
                    longrepr=longrepr,
                )
            )
    for test in data.get("tests", []):
        if test.get("outcome") not in ("failed", "error"):
            continue
        phase = test.get("call") or test.get("setup") or test.get("teardown") or {}
        longrepr = _text(phase.get("longrepr"))
        failures.append(
            failure(
                test.get("nodeid", "?"),
                _first_line(phase.get("longrepr")),
                frames=frames_from(phase, worktree),
                longrepr=longrepr,
            )
        )
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
        signature=report_signature(failures),
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
        lines.append(f"- {f.test_id} [{f.kind}] {f.message}")
        for fr in f.frames:
            if fr.in_repo:
                lines.append(f"    {fr.file}:{fr.line} in {fr.function}: {fr.code}")
    if len(report.failures) > max_failures:
        lines.append(f"… and {len(report.failures) - max_failures} more failures")
    return "\n".join(lines)
