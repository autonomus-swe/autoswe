"""Parse a JUnit XML report into the same :class:`TestReport` pytest's JSON produces.

Node and Go both emit JUnit — node 20's built-in `--test-reporter=junit`, and `gotestsum
--junitfile`. It is a thirty-year-old format from Ant, which is why every runner speaks it
and why none of them agree on the details.

## What has to survive the translation

The Debugger's prompt is built from `TestFailure.frames`: a file, a line, and the source
around it. A report that says only "TestAdd failed" tells it which test broke and nothing
about where, and it then spends its attempts reading files to find out.

JUnit has no frame list. What it has is a `<failure>` element whose *text* is the runner's
own output — a Go panic trace, a Node `AssertionError` with a stack. So the file and line
are recovered by reading that text, and the patterns below are per-runner because the text
is per-runner.

Where nothing parses, the failure still carries its message and an empty frame list, which
is what `environment_report` does too: less useful than a frame, much more useful than
silence.

## XML from a container is untrusted input

It is written by the repository's own test run, inside a sandbox, from code the agent may
have just edited. So it is parsed with `defusedxml` semantics in mind — no DTDs, no entity
expansion — via the stdlib parser with entity resolution left off, and every field is
bounded before it reaches a model's prompt.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from contracts import Frame, TestFailure, TestReport
from tools.test_report import classify, exc_type_of, report_signature, signature

MAX_MESSAGE = 2_000
MAX_FRAMES = 12

# node writes stack frames as `at TestContext.<anonymous> (file:///workspace/test/x.js:11:10)`
# — a file: URL, not a path. Measured; the scheme has to be in the pattern or the capture
# starts at `///workspace` and no amount of prefix-stripping recovers a real path.
_NODE_FRAME = re.compile(
    r"(?:file://)?(?P<file>/?[\w./\-]+\.(?:m?js|cjs|ts)):(?P<line>\d+)(?::\d+)?"
)
# go prints `    ops_test.go:13: Subtract(5, 3) = 2, want 99` — a BASENAME, never a path,
# so the file has to be found in the tree afterwards. A panic is the exception and carries
# an absolute container path, which the same pattern picks up.
_GO_FRAME = re.compile(r"(?P<file>[\w./\-]*\.go):(?P<line>\d+)")

# Failure messages that carry no information. gotestsum writes `message="Failed"` on every
# failure — assertion, panic, build error alike — and node writes `test failed` when a test
# file cannot even be loaded. Surfacing either as *the* message buries the real one.
_EMPTY_MESSAGES = frozenset({"failed", "test failed", "error", ""})


def _resolve(rel: str, worktree: Path | None, hint: str, cache: dict[str, str]) -> str:
    """A repo-relative path for what a runner named.

    node gives a real path under `/workspace`; go gives a bare `ops_test.go` and puts the
    package in the testcase's `classname`. So a basename is looked up in the tree, and the
    package hint breaks ties — `ops_test.go` in a repository with three of them resolves to
    the one in the package that failed.
    """
    stripped = rel.removeprefix("/workspace/").lstrip("/")
    if not worktree or "/" in stripped:
        return stripped
    if stripped in cache:
        return cache[stripped]
    matches = [p for p in worktree.rglob(stripped) if p.is_file()][:20]
    package = hint.rsplit("/", 1)[-1] if hint else ""
    best = next((p for p in matches if p.parent.name == package), None) or (
        matches[0] if matches else None
    )
    cache[stripped] = str(best.relative_to(worktree)) if best else stripped
    return cache[stripped]


def _frames(
    text: str, worktree: Path | None, hint: str = "", cache: dict[str, str] | None = None
) -> list[Frame]:
    """File/line pairs from a runner's failure text, nearest the assertion first.

    Both runners print the failing location before the library frames beneath it, so the
    order the patterns find them in is already the useful one.
    """
    out: list[Frame] = []
    seen: set[tuple[str, int]] = set()
    for pattern in (_GO_FRAME, _NODE_FRAME):
        for match in pattern.finditer(text):
            raw = match.group("file")
            line = int(match.group("line"))
            rel = _resolve(raw, worktree, hint, cache if cache is not None else {})
            if (rel, line) in seen:
                continue
            seen.add((rel, line))
            out.append(
                Frame(
                    file=rel,
                    line=line,
                    function="",
                    code=_source_line(worktree, rel, line),
                    # Node's stack includes `node:internal/...` frames and Go's includes
                    # the testing package. Only a file that is really in the checkout is
                    # somewhere the agent can edit, and `signature` keys on that.
                    in_repo=bool(worktree and (worktree / rel).is_file()),
                )
            )
            if len(out) >= MAX_FRAMES:
                return out
    return out


def _source_line(worktree: Path | None, rel: str, line: int) -> str:
    if not worktree:
        return ""
    try:
        lines = (worktree / rel).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return lines[line - 1].strip()[:200] if 0 < line <= len(lines) else ""


def _failure_text(element: Any) -> str:
    """The message attribute and the body, whichever the runner used.

    node puts a short reason in `message=` and the stack in the body; gotestsum leaves
    `message` as `Failed` and puts everything useful in the body. Taking both and letting
    the classifier read the pair is cheaper than guessing which runner wrote it.
    """
    attr = str(element.get("message") or "").strip()
    body = (element.text or "").strip()
    if attr.lower() in _EMPTY_MESSAGES:
        # gotestsum's constant "Failed". Keeping it would put the least informative word
        # available at the top of the Debugger's prompt.
        return body
    if attr and body and attr not in body:
        return f"{attr}\n{body}"
    return body or attr


def _test_id(case: Any) -> str:
    """`classname::name`, so it reads like the pytest ids everywhere else in the system."""
    name = str(case.get("name") or "<unnamed>")
    classname = str(case.get("classname") or "").strip()
    return f"{classname}::{name}" if classname else name


def parse_junit(
    xml: str, command: str, output: str = "", worktree: Path | None = None
) -> TestReport:
    """A JUnit report, or a report saying it could not be read.

    Raises nothing: a malformed report is a normal outcome when the thing that wrote it is
    the code under test.
    """
    try:
        # `forbid_dtd`-equivalent: the stdlib parser does not expand external entities by
        # default, and the report comes from a sandbox with no network regardless.
        root = ElementTree.fromstring(xml)  # noqa: S314 - see the module docstring
    except ElementTree.ParseError as e:
        from tools.test_report import environment_report

        return environment_report(command, f"could not parse the test report: {e}", output)

    cases = list(root.iter("testcase"))
    failures: list[TestFailure] = []
    resolved: dict[str, str] = {}
    skipped = 0
    errors = 0

    if not cases:
        # gotestsum answers a selector that matches no package with a well-formed report of
        # nothing — `<testsuites tests="0" failures="0" errors="1"/>` — and the explanation
        # goes only to stderr. `failures="0"` makes that look like a pass, which is the one
        # reading a harness must never take.
        from tools.test_report import environment_report

        return environment_report(
            command, "the test command matched no tests; check the selector", output
        )

    for case in cases:
        if case.find("skipped") is not None:
            skipped += 1
            continue
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
            if bad is None:
                continue
            errors += 1
        text = _failure_text(bad)[:MAX_MESSAGE]
        frames = _frames(text, worktree, str(case.get("classname") or ""), resolved)
        kind = classify(text)
        exc = exc_type_of(text)
        test_id = _test_id(case)
        failures.append(
            TestFailure(
                test_id=test_id,
                kind=kind,
                message=text,
                frames=frames,
                signature=signature(test_id, kind, exc, frames),
            )
        )

    failed = len(failures) - errors
    return TestReport(
        # Counted from the cases rather than read from the `tests=` attribute: node and
        # gotestsum disagree on whether a suite element repeats its children's counts, and
        # a total that does not match the failures listed under it is worse than no total.
        passed=not failures,
        total=len(cases),
        failed=max(failed, 0),
        errors=errors,
        skipped=skipped,
        failures=failures,
        duration_s=_duration(root),
        command=command,
        truncated_output=output[-4000:],
        signature=report_signature(failures),
    )


def _duration(root: Any) -> float:
    """The suite's own time, summed over top-level suites."""
    total = 0.0
    for suite in root.iter("testsuite"):
        try:
            total += float(suite.get("time") or 0.0)
        except ValueError:
            continue
    return round(total, 3)
