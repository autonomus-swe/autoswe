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
