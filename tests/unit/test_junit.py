"""Reading a JUnit report from Node and from Go.

Every sample below was produced by running the real runner in the real image, not written
by hand — which matters, because the two disagree about almost everything and the
disagreements are where a parser written from the spec goes wrong.

What has to survive is the *frame*: a file and a line. The Debugger's prompt is built from
those, and a report saying only "TestSubtract failed" sends it reading files to find out
where. That is the thing these tests are mostly about.
"""

# ruff: noqa: E501 - the XML below is verbatim runner output; reflowing it would make it
# a sample of something no runner emits.
from __future__ import annotations

from pathlib import Path

import pytest

from tools.junit import parse_junit

pytestmark = pytest.mark.unit

# node 20's `--test-reporter=junit`, captured from agent-sandbox:node-20. Note the totals
# are XML *comments*, there is no wrapping <testsuite> for bare `test()` calls, and the
# frame is a `file://` URL.
NODE_FAILING = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
\t<testcase name="add sums two numbers" time="0.004147" classname="test"/>
\t<testcase name="subtract handles negative results" time="0.004007" classname="test" failure="Expected values to be strictly equal:2 !== 3">
\t\t<failure type="testCodeFailure" message="Expected values to be strictly equal:2 !== 3">
[Error [ERR_TEST_FAILURE]: Expected values to be strictly equal:

2 !== 3
] {
  code: 'ERR_TEST_FAILURE',
  cause: AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:

  2 !== 3

      at TestContext.&lt;anonymous&gt; (file:///workspace/test/ops.test.js:11:10)
      at Test.runInAsyncScope (node:async_hooks:206:9)
      at Test.run (node:internal/test_runner/test:796:25)
}
\t\t</failure>
\t</testcase>
\t<!-- tests 2 -->
\t<!-- pass 1 -->
\t<!-- fail 1 -->
</testsuites>
"""

NODE_PASSING = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
\t<testcase name="add sums two numbers" time="0.005692" classname="test"/>
\t<testsuite name="slugify" time="0.007623" tests="2" failures="0" skipped="0">
\t\t<testcase name="lowercases and joins words" time="0.004385" classname="test"/>
\t\t<testcase name="collapses whitespace" time="0.000413" classname="test"/>
\t</testsuite>
\t<!-- tests 3 -->
</testsuites>
"""

# gotestsum's `--junitfile`, captured from agent-sandbox:go-1.23. `message` is the constant
# "Failed" on every failure, the payload is the element text, and the file is a BASENAME.
GO_FAILING = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="3" failures="1" errors="0" time="0.010369">
\t<testsuite tests="3" failures="1" time="0.010000" name="example.com/gofixture/ops">
\t\t<testcase classname="example.com/gofixture/ops" name="TestSubtract" time="0.000000">
\t\t\t<failure message="Failed" type="">=== RUN   TestSubtract&#xA;    ops_test.go:13: Subtract(5, 3) = 2, want 99&#xA;--- FAIL: TestSubtract (0.00s)&#xA;</failure>
\t\t</testcase>
\t\t<testcase classname="example.com/gofixture/ops" name="TestAdd" time="0.000000"></testcase>
\t</testsuite>
</testsuites>
"""

# A selector that matched no package. `failures="0"` is the trap.
GO_NOTHING_MATCHED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<testsuites tests="0" failures="0" errors="1" time="0.000"></testsuites>'
)


# ---- node ----------------------------------------------------------------------------------


def test_a_passing_node_run_reports_every_case() -> None:
    """Counted from the cases, not from an attribute: node puts its totals in XML comments
    and only wraps `describe()` blocks in a `<testsuite>`."""
    report = parse_junit(NODE_PASSING, "node --test")

    assert report.passed
    assert report.total == 3 and report.failed == 0
    assert report.failures == []


def test_a_node_failure_keeps_the_file_and_the_line(tmp_path: Path) -> None:
    """The whole point. `file:///workspace/test/ops.test.js:11:10` is a URL, and a pattern
    written for plain paths captures `///workspace/…`, which resolves to nothing."""
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "ops.test.js").write_text("\n" * 10 + "assert.equal(x, 3);\n")

    report = parse_junit(NODE_FAILING, "node --test", worktree=tmp_path)

    (failure,) = report.failures
    assert not report.passed and report.failed == 1
    top = failure.frames[0]
    assert top.file == "test/ops.test.js", failure.frames
    assert top.line == 11
    assert top.in_repo, "a file that is really in the checkout"
    assert "assert.equal" in top.code


def test_node_internal_frames_are_not_in_the_repository(tmp_path: Path) -> None:
    """`node:internal/test_runner/test:796` is not somewhere the agent can edit, and the
    failure's signature keys on the topmost frame that is."""
    report = parse_junit(NODE_FAILING, "node --test", worktree=tmp_path)

    (failure,) = report.failures
    assert not any(f.in_repo for f in failure.frames), "nothing exists in an empty tree"


def test_the_assertion_message_survives() -> None:
    report = parse_junit(NODE_FAILING, "node --test")

    (failure,) = report.failures
    assert "2 !== 3" in failure.message
    assert failure.kind == "assertion", failure.kind


# ---- go ------------------------------------------------------------------------------------


def test_a_go_failure_resolves_a_basename_to_a_path(tmp_path: Path) -> None:
    """go prints `ops_test.go:13` — a basename, never a path — and puts the package in the
    testcase's `classname`. Without resolving it the frame points at a file that is not
    there and `in_repo` is false, which is the same as having no frame at all."""
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "ops_test.go").write_text("\n" * 12 + "t.Errorf(...)\n")

    report = parse_junit(GO_FAILING, "gotestsum", worktree=tmp_path)

    (failure,) = report.failures
    top = failure.frames[0]
    assert top.file == "ops/ops_test.go", failure.frames
    assert top.line == 13 and top.in_repo


def test_the_package_breaks_a_tie_between_two_files_of_the_same_name(tmp_path: Path) -> None:
    """Two packages with an `ops_test.go` is the normal case in a Go repository."""
    for pkg in ("ops", "other"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "ops_test.go").write_text("\n" * 14)

    report = parse_junit(GO_FAILING, "gotestsum", worktree=tmp_path)

    assert report.failures[0].frames[0].file == "ops/ops_test.go"


def test_gotestsums_constant_message_is_not_surfaced() -> None:
    """`message="Failed"` is written on every failure — assertion, panic, build error — and
    carries nothing. Putting it at the top of the Debugger's prompt buries the real one."""
    report = parse_junit(GO_FAILING, "gotestsum")

    (failure,) = report.failures
    assert not failure.message.startswith("Failed")
    assert "want 99" in failure.message


def test_a_selector_that_matched_nothing_is_not_a_pass() -> None:
    """The trap: gotestsum answers a bad package pattern with a well-formed report of
    nothing, `failures="0"`, and puts the explanation only on stderr. Counting cases and
    finding none would otherwise read as a green run."""
    report = parse_junit(GO_NOTHING_MATCHED, "gotestsum -- ./nope/...")

    assert not report.passed
    assert report.errors == 1
    assert "matched no tests" in report.failures[0].message


# ---- both ----------------------------------------------------------------------------------


def test_a_report_the_test_run_corrupted_is_not_an_exception() -> None:
    """The thing that writes this file is the code under test."""
    report = parse_junit("<testsuites><testcase", "cmd", output="boom")

    assert not report.passed and report.errors == 1
    assert "could not parse" in report.failures[0].message
    assert report.truncated_output == "boom"


def test_two_runs_of_the_same_failure_share_a_signature(tmp_path: Path) -> None:
    """`signature` is how the transition table answers "same failure as last time", and it
    deliberately excludes the line so an edit above the failure does not change it."""
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "ops_test.go").write_text("\n" * 20)
    moved = GO_FAILING.replace("ops_test.go:13", "ops_test.go:17")

    first = parse_junit(GO_FAILING, "gotestsum", worktree=tmp_path)
    second = parse_junit(moved, "gotestsum", worktree=tmp_path)

    assert first.signature == second.signature
