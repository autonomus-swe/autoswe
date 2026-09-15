"""``ask_user``: the agent's own way to stop and ask, rather than guess.

A goal is sometimes genuinely ambiguous in a way the Planner did not catch, and guessing
produces work that gets thrown away. This tool costs a human's attention, so it requires
approval like any other interruption — the gate is what actually delivers the question and
brings back the answer.
"""

from __future__ import annotations

from typing import Any

from contracts import ToolResult
from tools.base import BaseTool, RunContext, schema

MAX_QUESTION_CHARS = 1000


class AskUserTool(BaseTool):
    name = "ask_user"
    description = (
        "Ask the human supervising this run one specific question, and wait for their "
        "answer. Use it when a decision is genuinely theirs to make and guessing wrong "
        "would waste the work — not for anything you could settle by reading the code."
    )
    input_schema = schema(
        {
            "question": {
                "type": "string",
                "description": "One specific question. State the options if there are any.",
            }
        },
        required=["question"],
    )
    # Not mutating: it changes nothing in the repository. It requires approval because it
    # spends something scarcer than a file write, which is somebody's attention.
    mutating = False
    parallel_safe = False
    requires_approval = True

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        """Only reached once the gate approved, and the gate supplies the answer.

        If execution gets here with nothing recorded, the question was approved but
        unanswered, which is a gate bug rather than something to paper over.
        """
        question = str(kwargs.get("question") or "").strip()[:MAX_QUESTION_CHARS]
        answer = ctx.answers.pop(question, None)
        if answer:
            return ToolResult(content=answer)
        return ToolResult(
            content="the question was approved but no answer was recorded", is_error=True
        )
