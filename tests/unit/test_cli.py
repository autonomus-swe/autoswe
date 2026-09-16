"""The pieces of `autoswe watch` that are worth pinning: frame parsing and summaries."""

from __future__ import annotations

import pytest

from cli.main import _describe, _frames

pytestmark = pytest.mark.unit


def wire(text: str) -> list[str]:
    """Split a stream the way httpx's ``iter_lines`` does, newline flavour included."""
    return text.replace("\r\n", "\n").split("\n")[:-1]


def test_frames_reads_the_crlf_the_server_actually_sends() -> None:
    """sse-starlette separates with CRLF; splitting on LF alone yields nothing at all."""
    stream = (
        "id: 1\r\nevent: phase_changed\r\ndata: {}\r\n\r\n"
        "id: 2\r\nevent: run_finished\r\ndata: {}\r\n\r\n"
    )
    assert [(i, t) for i, t, _ in _frames(wire(stream))] == [
        ("1", "phase_changed"),
        ("2", "run_finished"),
    ]


def test_frames_keeps_the_id_for_a_reconnect_and_ignores_unknown_fields() -> None:
    stream = 'id: 7\r\nretry: 3000\r\nevent: tool_call\r\ndata: {"name": "run_tests"}\r\n\r\n'
    ((event_id, type_, data),) = list(_frames(wire(stream)))
    assert event_id == "7" and type_ == "tool_call" and data == '{"name": "run_tests"}'


def test_frames_joins_multi_line_data_and_defaults_the_type() -> None:
    stream = "data: line one\r\ndata: line two\r\n\r\n"
    ((_, type_, data),) = list(_frames(wire(stream)))
    assert type_ == "message" and data == "line one\nline two"


def test_frames_drops_a_trailing_partial_frame() -> None:
    """A stream cut mid-frame must not yield half an event."""
    assert list(_frames(wire("id: 1\r\nevent: a\r\ndata: {}\r\n\r\nid: 2\r\nevent: b\r\n"))) == [
        ("1", "a", "{}")
    ]


@pytest.mark.parametrize(
    ("type_", "payload", "expected"),
    [
        ("phase_changed", {"phase": "code"}, "code"),
        ("phase_changed", {"phase": "code", "tasks": 3}, "code (3 tasks)"),
        ("agent_started", {"agent": "coder", "task_id": "t2"}, "coder on t2"),
        ("agent_started", {"agent": "planner"}, "planner"),
        ("agent_finished", {"error": "boom"}, "error: boom"),
        ("agent_finished", {}, "finished"),
        ("tool_call", {"name": "run_tests", "is_error": True}, "run_tests — failed"),
        (
            "test_report",
            {"passed": False, "total": 9, "failed": 2},
            "failed — 9 tests, 2 failing",
        ),
        (
            "test_report",
            {"baseline": True, "passed": False, "total": 9, "failed": 2},
            # not a verdict on the agent's work: it is what the repository already was
            "baseline — 9 tests, 2 failing",
        ),
        (
            "test_report",
            {
                "passed": True,
                "total": 9,
                "failed": 0,
                "flaky": ["tests/z.py::test_a"],
                "pre_existing": ["tests/y.py::test_b", "tests/y.py::test_c"],
            },
            "passed — 9 tests, 0 failing · 1 flaky · 2 pre-existing",
        ),
        ("awaiting_input", {"questions": ["a?", "b?"]}, "a? · b?"),
        ("run_finished", {"status": "done", "cost_usd": 0.1234}, "done · $0.1234"),
        ("run_finished", {"status": "failed"}, "failed · $0.0000"),
        ("pr_opened", {"pr_url": "https://x/pull/1"}, "https://x/pull/1"),
    ],
)
def test_describe_summarises_every_event_the_run_emits(
    type_: str, payload: dict[str, object], expected: str
) -> None:
    assert _describe(type_, payload) == expected


def test_describe_never_raises_on_a_payload_it_has_not_seen() -> None:
    assert _describe("something_new", {}) == ""
