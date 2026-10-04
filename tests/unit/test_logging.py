from __future__ import annotations

import io
import json
import uuid

import pytest

from observability.logging import bind_run, clear_run, configure_logging, get_logger

pytestmark = pytest.mark.unit


def _capture(level: str = "INFO") -> io.StringIO:
    buf = io.StringIO()
    configure_logging(level, json=True, stream=buf)
    clear_run()
    return buf


def _records(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_sensitive_keys_and_values_are_redacted() -> None:
    buf = _capture()
    get_logger().info(
        "call",
        api_key="sk-ant-abcdefghijklmnop",
        github_token="ghp_abcdefghijklmnop",
        note="token was ghp_abcdefghijklmnop and sk-ant-zzzzzzzzzzzz",
        dsn="postgresql+asyncpg://user:pw@host:5432/db",
        input_tokens=12,
        cache_read_tokens=3,
    )
    (rec,) = _records(buf)
    assert rec["api_key"] == "[redacted]" and rec["github_token"] == "[redacted]"
    assert "ghp_" not in str(rec["note"]) and "sk-ant-" not in str(rec["note"])
    assert rec["dsn"] == "postgresql+asyncpg://[redacted]@host:5432/db"
    assert rec["input_tokens"] == 12 and rec["cache_read_tokens"] == 3
    assert rec["event"] == "call" and rec["level"] == "info" and "timestamp" in rec


def test_bound_run_context_appears_and_clears() -> None:
    buf = _capture()
    run_id = uuid.uuid4()
    bind_run(run_id, task_id="t1", step_id=uuid.uuid4())
    get_logger().info("one")
    clear_run()
    get_logger().info("two")
    first, second = _records(buf)
    assert first["run_id"] == str(run_id) and first["task_id"] == "t1" and "step_id" in first
    assert "run_id" not in second


def test_level_filtering() -> None:
    buf = _capture("WARNING")
    log = get_logger()
    log.info("hidden")
    log.warning("shown")
    recs = _records(buf)
    assert [r["event"] for r in recs] == ["shown"]


def test_a_sensitive_key_is_redacted_even_when_its_value_matches_no_pattern() -> None:
    """The half `test_sensitive_keys_and_values_are_redacted` cannot prove.

    That test passes `api_key="sk-ant-…"` and `github_token="ghp_…"` — values which the
    *pattern* branch also catches. So disabling the key branch entirely leaves it green:
    the keys are still redacted, by the other mechanism. Measured, not supposed — the
    mutation survived the whole unit suite.

    Two mechanisms, and only one of them tested. The key branch is the one that matters
    most, because it is what covers a secret whose shape nobody anticipated: an internal
    token, a rotated format, a vendor that changed its prefix. The patterns can only catch
    what they were written for; `SENSITIVE_KEYS` catches anything a caller was honest
    enough to name.

    So the value here is deliberately unremarkable — no `sk-`, no `ghp_`, no URL — and
    redaction has to come from the key alone.
    """
    buf = _capture()
    get_logger().info(
        "call",
        api_key="unremarkable",
        password="also-unremarkable",
        authorization="nothing-pattern-like-here",
        github_token="plain",
        safe_field="visible",
    )
    (rec,) = _records(buf)

    for key in ("api_key", "password", "authorization", "github_token"):
        assert rec[key] == "[redacted]", f"{key} leaked a value no pattern would have caught"
    assert rec["safe_field"] == "visible", "and an ordinary field is left alone"


def test_a_sensitive_suffix_is_redacted_on_a_key_nobody_listed() -> None:
    """`SENSITIVE_SUFFIXES` is what makes the list extensible without being edited.

    A field called `openrouter_api_key` is not in `SENSITIVE_KEYS` and never will be —
    there is a new provider every month. The suffix rule is what covers it, and the same
    unremarkable value is used so the patterns cannot do the work.
    """
    buf = _capture()
    get_logger().info("call", openrouter_api_key="unremarkable", vendor_token="plain")
    (rec,) = _records(buf)

    assert rec["openrouter_api_key"] == "[redacted]"
    assert rec["vendor_token"] == "[redacted]"


def test_the_event_name_is_never_redacted_whatever_it_is_called() -> None:
    """An event named `token_refreshed` is the name of a thing that happened, not a secret.

    The property holds for a reason worth stating precisely, because the obvious reading is
    wrong: it is **not** the `key != "event"` clause in `drop_sensitive` that saves it.
    `_is_sensitive_key("event")` is `False` — "event" is in neither `SENSITIVE_KEYS` nor the
    suffix list — so that clause can never change the outcome. Removing it survives this
    test and every other, because there is no input on which it matters.

    What does the work is the second branch: the event name is a string, so it goes through
    the pattern substitution like any other, and is redacted only if it genuinely looks like
    a secret. Which is the behaviour you want — `log.info("ghp_abc…")` should not be printed
    either.

    So what is the clause for? A future edit. Add "event" to `SENSITIVE_KEYS`, or a suffix
    that "event" happens to match, and without it **every log line in the system loses its
    event name** — the single field that says what happened. The test below creates exactly
    that condition, which is the only way to make the clause reachable and therefore the
    only way to hold it.
    """
    buf = _capture()
    get_logger().info("token_refreshed")
    (rec,) = _records(buf)
    assert rec["event"] == "token_refreshed"

    buf = _capture()
    get_logger().info("secret")
    (rec,) = _records(buf)
    assert rec["event"] == "secret", "an event *named* secret is still an event name"


def test_the_event_name_survives_even_if_event_became_a_sensitive_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clause `key != "event"` exists for this and only this.

    It cannot be reached today — `_is_sensitive_key("event")` is `False`, so removing the
    clause changes nothing and no mutation of it can be caught by ordinary input. What it
    defends against is somebody adding "event" to `SENSITIVE_KEYS`, or adding a suffix that
    "event" matches, at which point **every log line in the system loses the field that says
    what happened** and the loss is invisible because the lines still parse.

    Creating that condition is what makes the guard testable. Without this test the clause
    is indistinguishable from dead code and gets deleted by the next simplification — which
    is precisely when it would have started mattering.
    """
    from observability import logging as obs

    monkeypatch.setattr(obs, "SENSITIVE_KEYS", frozenset({*obs.SENSITIVE_KEYS, "event"}))

    buf = _capture()
    get_logger().info("run_started")
    (rec,) = _records(buf)

    assert rec["event"] == "run_started", (
        "the event name was redacted; with `event` sensitive and no guard, every log line "
        "in the system loses the field that says what happened"
    )
