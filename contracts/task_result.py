from __future__ import annotations

from contracts.common import LLMModel


class TaskResult(LLMModel):
    summary: str
    files_touched: list[str]
    how_to_test: str
    notes_for_reviewer: list[str]
