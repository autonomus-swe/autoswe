from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import FakeSandbox, make_ctx, ok
from tools.test_report import guess_kind, parse_json_report, summarize
from tools.tests import RunTestsTool
from tools.tests import test_command as build_test_command

pytestmark = pytest.mark.unit

ALL_PASS = {
    "duration": 0.12,
    "summary": {"total": 3, "passed": 3, "collected": 3},
    "tests": [{"nodeid": f"tests/test_ops.py::test_{i}", "outcome": "passed"} for i in range(3)],
    "collectors": [{"nodeid": "tests/test_ops.py", "outcome": "passed"}],
}
ONE_FAIL = {
    "duration": 0.2,
    "summary": {"total": 3, "passed": 2, "failed": 1},
    "tests": [
        {"nodeid": "tests/test_ops.py::test_add", "outcome": "passed"},
        {
            "nodeid": "tests/test_ops.py::test_subtract",
            "outcome": "failed",
            "call": {
                "longrepr": (
                    "def test_subtract():\n>       assert subtract(3, 1) == 2\n"
                    "E       AssertionError: assert 3 == 2"
                )
            },
        },
        {"nodeid": "tests/test_ops.py::test_slugify", "outcome": "passed"},
    ],
    "collectors": [],
}
COLLECTION_ERROR = {
    "duration": 0.05,
    "summary": {"total": 0, "error": 1},
    "tests": [],
    "collectors": [
        {
            "nodeid": "tests/test_ops.py",
            "outcome": "failed",
            "longrepr": (
                "ImportError while importing test module\n"
                "ModuleNotFoundError: No module named 'fixture.ops'"
            ),
        }
    ],
}


def test_all_pass() -> None:
    r = parse_json_report(ALL_PASS, "pytest")
    assert r.passed and r.total == 3 and r.failures == [] and r.command == "pytest"
    assert summarize(r).startswith("3 passed, 0 failed") and "ALL TESTS PASS" in summarize(r)


def test_one_assertion_failure() -> None:
    r = parse_json_report(ONE_FAIL, "pytest")
    assert not r.passed and r.failed == 1 and len(r.failures) == 1
    f = r.failures[0]
    assert f.test_id.endswith("test_subtract") and f.kind == "assertion"
    assert f.message == "AssertionError: assert 3 == 2" and len(f.signature) == 16
    assert "- tests/test_ops.py::test_subtract [assertion]" in summarize(r)


def test_collection_error_is_import_kind() -> None:
    r = parse_json_report(COLLECTION_ERROR, "pytest")
    assert not r.passed and r.errors == 1 and r.failures[0].kind == "import"


def test_no_tests_collected_is_environment() -> None:
    r = parse_json_report({"summary": {"total": 0}, "tests": [], "collectors": []}, "pytest")
    assert not r.passed and r.failures[0].kind == "environment"


@pytest.mark.parametrize(
    ("msg", "kind"),
    [
        ("AssertionError: x", "assertion"),
        ("assert 1 == 2", "assertion"),
        ("ModuleNotFoundError: y", "import"),
        ("Failed: Timeout > 5s", "timeout"),
        ("ZeroDivisionError: division by zero", "exception"),
    ],
)
def test_guess_kind(msg: str, kind: str) -> None:
    assert guess_kind(msg) == kind


def test_command_shape() -> None:
    assert build_test_command("uv run --no-sync pytest -q", "") == (
        "uv run --no-sync pytest -q --json-report --json-report-file=.autoswe/report.json "
        "-p no:cacheprovider"
    )
    assert " tests/test_x.py " in build_test_command("pytest", "tests/test_x.py")


async def test_run_tests_tool_reads_report_and_handles_missing(tmp_path: Path) -> None:
    import json

    def handler(cmd: str) -> object:
        (tmp_path / ".autoswe").mkdir(exist_ok=True)
        (tmp_path / ".autoswe" / "report.json").write_text(json.dumps(ONE_FAIL))
        return ok("1 failed, 2 passed", exit_code=1)

    sb = FakeSandbox(tmp_path, handler)  # type: ignore[arg-type]
    res = await RunTestsTool()(make_ctx(tmp_path, sb), selector="tests/test_ops.py")
    assert res.is_error and res.artifact and res.artifact["failed"] == 1
    assert "--json-report" in sb.commands[0] and "tests/test_ops.py" in sb.commands[0]
    assert "--- output (tail) ---" in res.content

    missing = await RunTestsTool()(make_ctx(tmp_path, FakeSandbox(tmp_path, lambda c: ok("", 2))))
    assert (
        missing.is_error
        and missing.artifact
        and missing.artifact["failures"][0]["kind"] == "environment"
    )


# ---- a collection failure: measured against what pytest actually emits --------------

COLLECTION_SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "reports"


def test_a_module_that_will_not_import_is_classified_as_import() -> None:
    """The real report from `b-missing-import`, not a hand-written approximation.

    A forgotten `from datetime import datetime` arrives as a bare NameError: the json
    report carries the crash message, not pytest's "ImportError while importing test
    module" header. Classifying that as `exception` tells the Debugger nothing, so a
    collection failure with no more specific class is an `import` failure — the one thing
    we do know is that the module would not load.
    """
    data = json.loads((COLLECTION_SAMPLE / "collection_nameerror.json").read_text())
    report = parse_json_report(data, "pytest -q")

    assert not report.passed and report.errors == 1
    (failure,) = report.failures
    assert failure.kind == "import"
    assert failure.test_id == "tests/test_stamp.py"
    assert "datetime" in failure.message


def test_a_collection_failure_still_carries_its_frames(tmp_path: Path) -> None:
    """A collector has no `traceback` in the json report, only a printed longrepr — and a
    Debugger handed a message with no frames has nothing to read."""
    (tmp_path / "chaos").mkdir()
    (tmp_path / "chaos" / "stamp.py").write_text("\n".join(f"line {i}" for i in range(1, 9)))
    data = json.loads((COLLECTION_SAMPLE / "collection_nameerror.json").read_text())
    report = parse_json_report(data, "pytest -q", worktree=tmp_path)

    frames = report.failures[0].frames
    assert [f.file for f in frames] == ["tests/test_stamp.py", "chaos/stamp.py"]
    assert frames[1].line == 5 and frames[1].function == "<module>"
    assert frames[1].code == "line 5", "the source is read from disk, never from the report"


def test_a_collection_failure_with_a_specific_cause_keeps_it() -> None:
    """`import` is the fallback, not an override: a module-level ConnectionError is an
    environment failure whether it happened during collection or during a test."""
    data = {
        "duration": 0.1,
        "summary": {"total": 0, "error": 1},
        "tests": [],
        "collectors": [
            {
                "nodeid": "tests/test_net.py",
                "outcome": "failed",
                "longrepr": (
                    "tests/test_net.py:2: in <module>\n    probe()\n"
                    "E   ConnectionRefusedError: [Errno 111] Connection refused"
                ),
            }
        ],
    }
    assert parse_json_report(data, "pytest -q").failures[0].kind == "environment"


# ---- source code is not a classification ---------------------------------------------

# Exactly what `d-network` produced inside a real sandbox: a DNS failure, with the word
# "timeout" appearing only in the *code* that caused it.
NO_NETWORK = {
    "duration": 0.18,
    "summary": {"total": 4, "passed": 3, "failed": 1},
    "tests": [
        {
            "nodeid": "tests/test_fetch.py::test_example_com_is_reachable",
            "outcome": "failed",
            "call": {
                "longrepr": (
                    "    def test_example_com_is_reachable():\n"
                    '>       assert title_length("https://example.com") > 100\n'
                    "tests/test_fetch.py:5: in test_example_com_is_reachable\n"
                    '    assert title_length("https://example.com") > 100\n'
                    "chaos/fetch.py:10: in title_length\n"
                    "    with urlopen(url, timeout=5) as response:\n"
                    "E   socket.gaierror: [Errno -3] Temporary failure in name resolution"
                )
            },
        }
    ],
    "collectors": [],
}


def test_a_dns_failure_is_environment_even_when_the_code_says_timeout() -> None:
    """Found in a sandbox, not at a desk. `urlopen(url, timeout=5)` is the line that
    *caused* the failure, not a description of it — and reading both at once let it
    outvote `socket.gaierror`.

    It matters which way round this goes: the Debugger's prompt tells it that
    `environment` failures are not its to fix, and says nothing of the sort about
    `timeout`. Misclassified, the one scenario built to stop it mocking the network was
    the one that invited it.
    """
    report = parse_json_report(NO_NETWORK, "pytest -q")
    (failure,) = report.failures
    assert failure.kind == "environment"
    assert "name resolution" in failure.message


def test_a_real_timeout_is_still_a_timeout() -> None:
    """The fix must not cost the classification it was protecting."""
    assert guess_kind("Failed: Timeout >5.0s") == "timeout"
    assert guess_kind("E   TimeoutError: the read timed out") == "timeout"
    assert guess_kind("subprocess.TimeoutExpired: command timed out") == "timeout"


def test_the_message_outranks_the_traceback() -> None:
    from tools.test_report import classify

    assert classify("ValueError: bad width", "with urlopen(url, timeout=5):") == "exception"
    assert classify("", "E   ModuleNotFoundError: No module named 'x'") == "import", (
        "the traceback is still read when the message says nothing"
    )
