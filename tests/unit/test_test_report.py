from __future__ import annotations

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
    assert f.message == "AssertionError: assert 3 == 2" and len(f.signature) == 40
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
