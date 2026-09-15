"""Parsing v2: the evidence a Debugger reasons from, and a signature that survives edits.

The payloads here follow pytest-json-report's shape — a ``call`` phase with ``traceback``
entries, a ``crash``, and the long representation that is the only place a function name
appears.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from contracts import Frame
from repo import source_context as src
from tools.test_report import (
    exc_type_of,
    frames_from,
    guess_kind,
    parse_json_report,
    report_signature,
    signature,
)

pytestmark = pytest.mark.unit


def worktree(tmp_path: Path) -> Path:
    """A worktree with one source file and one test, plus a vendored package."""
    (tmp_path / "fixture").mkdir()
    (tmp_path / "fixture" / "ops.py").write_text(
        "def paginate(items, size):\n"
        "    out = []\n"
        "    for i in range(0, len(items) - 1, size):\n"
        "        out.append(items[i : i + size])\n"
        "    return out\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ops.py").write_text(
        "from fixture.ops import paginate\n\n\n"
        "def test_last_page():\n"
        "    assert paginate([1, 2, 3], 2) == [[1, 2], [3]]\n"
    )
    vendored = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "pytest"
    vendored.mkdir(parents=True)
    (vendored / "python.py").write_text("def pytest_pyfunc_call(pyfuncitem):\n    raise\n")
    return tmp_path


LONGREPR = """\
tests/test_ops.py:5: in test_last_page
    assert paginate([1, 2, 3], 2) == [[1, 2], [3]]
fixture/ops.py:3: in paginate
    for i in range(0, len(items) - 1, size):
E   AssertionError: assert [[1, 2]] == [[1, 2], [3]]
"""


def assertion_payload(tmp: Path, *, line: int = 3) -> dict[str, Any]:
    """One failure whose traceback crosses the test, the source, and a vendored frame."""
    return {
        "summary": {"total": 1, "failed": 1},
        "duration": 0.12,
        "tests": [
            {
                "nodeid": "tests/test_ops.py::test_last_page",
                "outcome": "failed",
                "call": {
                    "longrepr": LONGREPR,
                    "traceback": [
                        {"path": "tests/test_ops.py", "lineno": 5, "message": ""},
                        {"path": "fixture/ops.py", "lineno": line, "message": ""},
                        {
                            "path": ".venv/lib/python3.12/site-packages/pytest/python.py",
                            "lineno": 2,
                            "message": "",
                        },
                    ],
                    "crash": {
                        "path": "fixture/ops.py",
                        "lineno": line,
                        "message": "AssertionError: assert [[1, 2]] == [[1, 2], [3]]",
                    },
                },
            }
        ],
    }


def test_frames_carry_location_function_and_the_real_source_line(tmp_path: Path) -> None:
    wt = worktree(tmp_path)
    report = parse_json_report(assertion_payload(wt), "pytest -q", "", worktree=wt)
    (fail,) = report.failures
    assert fail.kind == "assertion"
    assert [f.file for f in fail.frames] == [
        "tests/test_ops.py",
        "fixture/ops.py",
        ".venv/lib/python3.12/site-packages/pytest/python.py",
    ]
    assert [f.in_repo for f in fail.frames] == [True, True, False]
    # the function name exists only in the long representation
    assert [f.function for f in fail.frames[:2]] == ["test_last_page", "paginate"]
    # the code comes off disk, never from the payload
    assert fail.frames[1].code == "for i in range(0, len(items) - 1, size):"
    # nothing is read for a vendored frame: it is not the agent's to fix
    assert fail.frames[2].code == ""


def test_the_signature_survives_the_line_moving(tmp_path: Path) -> None:
    """A Debugger edits code and lines shift. Same test, same fault, same signature."""
    wt = worktree(tmp_path)
    before = parse_json_report(assertion_payload(wt, line=3), "pytest -q", worktree=wt)
    after = parse_json_report(assertion_payload(wt, line=9), "pytest -q", worktree=wt)
    assert before.failures[0].signature == after.failures[0].signature
    assert before.signature == after.signature


def test_the_signature_keys_on_the_outermost_in_repo_frame(tmp_path: Path) -> None:
    """Which frame the signature keys on decides what "the same failure" means.

    It is the *topmost* in-repo frame, which for pytest is the test function itself. So
    moving the fault deeper does not change the signature — deliberately: the test is
    still failing the same way, which is exactly when the Debugger should be told its last
    hypothesis achieved nothing. Keying on the innermost frame would reset that signal
    every time the Debugger moved the bug around.
    """
    wt = worktree(tmp_path)
    same = parse_json_report(assertion_payload(wt), "pytest -q", worktree=wt)
    moved = assertion_payload(wt)
    moved["tests"][0]["call"]["longrepr"] = LONGREPR.replace("in paginate", "in chunk")
    deeper = parse_json_report(moved, "pytest -q", worktree=wt)
    assert same.failures[0].signature == deeper.failures[0].signature

    # but the test function itself changing is a different failure
    renamed = assertion_payload(wt)
    renamed["tests"][0]["call"]["longrepr"] = LONGREPR.replace("in test_last_page", "in test_first")
    assert (
        parse_json_report(renamed, "pytest -q", worktree=wt).failures[0].signature
        != same.failures[0].signature
    )


def test_the_signature_changes_when_the_exception_type_changes(tmp_path: Path) -> None:
    wt = worktree(tmp_path)
    base = parse_json_report(assertion_payload(wt), "pytest -q", worktree=wt)
    other = assertion_payload(wt)
    other["tests"][0]["call"]["longrepr"] = LONGREPR.replace(
        "E   AssertionError: assert", "E   ValueError: assert"
    )
    changed = parse_json_report(other, "pytest -q", worktree=wt)
    assert base.failures[0].signature != changed.failures[0].signature


def test_a_signature_ignores_frames_outside_the_repository(tmp_path: Path) -> None:
    """The topmost in-repo frame is the key; library frames differ between environments."""
    wt = worktree(tmp_path)
    payload = assertion_payload(wt)
    baseline = parse_json_report(payload, "pytest -q", worktree=wt).failures[0].signature
    payload["tests"][0]["call"]["traceback"][2]["path"] = "/usr/lib/python3.12/unittest/case.py"
    shifted = parse_json_report(payload, "pytest -q", worktree=wt).failures[0].signature
    assert baseline == shifted


def test_collection_failure_is_an_import_kind_with_the_exception_type(tmp_path: Path) -> None:
    wt = worktree(tmp_path)
    data = {
        "summary": {"total": 0},
        "collectors": [
            {
                "nodeid": "tests/test_new.py",
                "outcome": "failed",
                "longrepr": (
                    "tests/test_new.py:1: in <module>\n"
                    "E   ModuleNotFoundError: No module named 'dateutil'"
                ),
                "traceback": [{"path": "tests/test_new.py", "lineno": 1}],
            }
        ],
    }
    report = parse_json_report(data, "pytest -q", worktree=wt)
    (fail,) = report.failures
    assert fail.kind == "import" and report.errors == 1 and not report.passed
    assert fail.frames and fail.frames[0].in_repo


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("AssertionError: assert 3 == 2", "assertion"),
        ("E   assert paginate([1]) == []", "assertion"),
        ("ModuleNotFoundError: No module named 'dateutil'", "import"),
        ("ImportError: cannot import name 'Stats'", "import"),
        ("Failed: Timeout >60.0s", "timeout"),
        ("ConnectionRefusedError: [Errno 111] Connection refused", "environment"),
        ("PermissionError: [Errno 13] Permission denied: '/etc/hosts'", "environment"),
        ("socket.gaierror: Name or service not known", "environment"),
        ("ValueError: bad input", "exception"),
    ],
)
def test_kind_classification(message: str, kind: str) -> None:
    assert guess_kind(message) == kind


def test_import_is_classified_before_assertion_when_both_appear() -> None:
    """A collection failure often mentions an assert somewhere in its traceback."""
    assert guess_kind("E   assert False\nModuleNotFoundError: No module named 'x'") == "import"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("E   AssertionError: assert 1 == 2", "AssertionError"),
        ("ValueError: bad input", "ValueError"),
        ("E   socket.gaierror: nope", "socket.gaierror"),
        ("something went wrong", ""),
    ],
)
def test_exception_type_extraction(text: str, expected: str) -> None:
    assert exc_type_of(text) == expected


def test_report_signature_is_order_independent() -> None:
    from tools.test_report import failure

    a, b = failure("t::one", "AssertionError: x"), failure("t::two", "ValueError: y")
    assert report_signature([a, b]) == report_signature([b, a])
    assert report_signature([a]) != report_signature([a, b])


def test_frames_tolerate_a_payload_with_nothing_usable(tmp_path: Path) -> None:
    """A malformed traceback must not raise while a run is being diagnosed."""
    assert frames_from({}, tmp_path) == []
    assert frames_from({"traceback": [{"path": "", "lineno": "x"}]}, tmp_path) == []
    odd = frames_from({"traceback": [{"path": "gone.py", "lineno": None}]}, tmp_path)
    assert len(odd) == 1 and odd[0].line == 0 and odd[0].code == ""


def test_signature_without_frames_still_differs_by_test_and_kind() -> None:
    assert signature("t::a", "assertion") != signature("t::b", "assertion")
    assert signature("t::a", "assertion") != signature("t::a", "import")
    assert len(signature("t::a", "assertion")) == 16


# ---- source context ----------------------------------------------------------------


def test_source_context_marks_the_failing_line(tmp_path: Path) -> None:
    wt = worktree(tmp_path)
    out = src.around("fixture/ops.py", 3, wt, radius=1)
    assert out.splitlines() == [
        "  2 |     out = []",
        "> 3 |     for i in range(0, len(items) - 1, size):",
        "  4 |         out.append(items[i : i + size])",
    ]


def test_source_context_is_best_effort(tmp_path: Path) -> None:
    wt = worktree(tmp_path)
    assert src.around("does/not/exist.py", 3, wt) == ""
    assert src.around("fixture/ops.py", 9999, wt, radius=1) == ""
    assert src.line_at("fixture/ops.py", 9999, wt) == ""
    assert src.line_at("does/not/exist.py", 1, wt) == ""


def test_in_repo_rejects_escapes_and_vendored_paths(tmp_path: Path) -> None:
    wt = worktree(tmp_path)
    assert src.in_repo("fixture/ops.py", wt)
    assert not src.in_repo("../outside.py", wt)
    assert not src.in_repo("/etc/passwd", wt)
    assert not src.in_repo(".venv/lib/python3.12/site-packages/pytest/python.py", wt)
    assert not src.in_repo("", wt)


def test_frame_model_accepts_what_the_parser_builds() -> None:
    f = Frame(file="a.py", line=1, function="f", code="pass", in_repo=True)
    assert f.in_repo and f.function == "f"
