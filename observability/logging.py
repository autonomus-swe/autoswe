"""structlog configuration: JSON lines with run/task/step ids bound via contextvars, and a
redaction processor so secrets never reach a log line (README §9, security rules)."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, TextIO
from uuid import UUID

import structlog
from structlog.typing import EventDict

SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "token",
        "authorization",
        "password",
        "secret",
        "anthropic_api_key",
        "github_token",
        "database_url",
        "redis_url",
        "x-api-key",
        "cookie",
    }
)
SENSITIVE_SUFFIXES: tuple[str, ...] = (
    "_key",
    "_token",
    "_secret",
    "_password",
    "password",
    "secret",
)
SECRET_PATTERNS = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|AKIA[0-9A-Z]{16}|(?<=://)[^:/@\s]+:[^@\s]+(?=@))"
)
REDACTED = "[redacted]"


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return k in SENSITIVE_KEYS or k.endswith(SENSITIVE_SUFFIXES)


def drop_sensitive(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        value = event_dict[key]
        if key != "event" and _is_sensitive_key(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = SECRET_PATTERNS.sub(REDACTED, value)
    return event_dict


def configure_logging(
    level: str = "INFO", *, json: bool = True, stream: TextIO | None = None
) -> None:
    level_int = logging.getLevelName(level.upper())
    if not isinstance(level_int, int):
        level_int = logging.INFO
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            drop_sensitive,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def bind_run(
    run_id: UUID | str, task_id: str | None = None, step_id: UUID | str | None = None
) -> None:
    fields: dict[str, str] = {"run_id": str(run_id)}
    if task_id is not None:
        fields["task_id"] = task_id
    if step_id is not None:
        fields["step_id"] = str(step_id)
    structlog.contextvars.bind_contextvars(**fields)


def clear_run() -> None:
    structlog.contextvars.clear_contextvars()


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name) if name else structlog.get_logger()
