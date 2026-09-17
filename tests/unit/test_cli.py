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


# ---- `autoswe artifacts` ----------------------------------------------------------------


def _invoke(monkeypatch: pytest.MonkeyPatch, args: list[str], responses: dict[str, object]) -> str:
    """Run the `artifacts` command against a stubbed HTTP client, return its stdout.

    Stubbed at the client rather than at `_check`, because the behaviour worth pinning is
    the content-type branch: the diff comes back as text/plain and everything else as JSON,
    and a command that called `.json()` on a patch would report a parse error instead of
    printing it.
    """
    import typer.testing

    from cli import main

    class Response:
        def __init__(self, payload: object) -> None:
            self.payload = payload
            is_text = isinstance(payload, str)
            self.headers = {"content-type": "text/plain" if is_text else "application/json"}
            self.text = payload if isinstance(payload, str) else ""
            self.status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return self.payload

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def get(self, path: str) -> Response:
            assert path in responses, f"the command asked for {path}, which is not stubbed"
            return Response(responses[path])

    monkeypatch.setattr(main, "_client", lambda api, key: Client())
    result = typer.testing.CliRunner().invoke(main.app, ["artifacts", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_artifacts_lists_kinds_with_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(
        monkeypatch,
        ["run-1"],
        {
            "/runs/run-1/artifacts": [
                {"kind": "diff", "size": 324, "created_at": "2026-09-18T01:00:00Z"},
                {"kind": "security", "size": 91, "created_at": "2026-09-18T01:01:00Z"},
            ]
        },
    )

    assert "diff" in out and "324" in out
    assert "security" in out and "91" in out


def test_artifacts_prints_json_that_jq_can_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The phase document's own example is `autoswe artifacts <id> security | jq .checklist`,
    so the output has to parse as JSON and not as a Python repr."""
    import json

    out = _invoke(
        monkeypatch,
        ["run-1", "security"],
        {"/runs/run-1/artifacts/security": {"checklist": {"no_secrets": True}}},
    )

    assert json.loads(out) == {"checklist": {"no_secrets": True}}


def test_artifacts_prints_a_diff_as_the_patch_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Served as text/plain so it can be piped to `git apply`. JSON-escaping every line of
    a patch helps nobody."""
    out = _invoke(
        monkeypatch, ["run-1", "diff"], {"/runs/run-1/artifacts/diff": "--- a\n+++ b\n+x = 1\n"}
    )

    assert out.startswith("--- a\n+++ b\n")


def test_artifacts_says_so_when_a_run_wrote_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty list printing nothing at all reads as a broken command."""
    assert "no artifacts" in _invoke(monkeypatch, ["run-1"], {"/runs/run-1/artifacts": []})
