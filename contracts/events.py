from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from contracts.common import StateModel

EventType = Literal[
    "phase_changed",
    "agent_started",
    "agent_finished",
    "agent_text",
    "tool_call",
    "tool_result",
    "test_report",
    "debug_hypothesis",
    "review_report",
    "security_report",
    "awaiting_input",
    "pr_opened",
    "budget_warning",
    "run_finished",
    "log",
]


class Event(StateModel):
    run_id: UUID
    type: EventType
    ts: datetime
    payload: dict[str, Any]
