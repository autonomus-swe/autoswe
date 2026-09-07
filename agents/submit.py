"""``submit_*`` tools: the one way an agent ends with a validated structured object."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ValidationError

from contracts import LLMModel, ToolResult
from tools.base import BaseTool, RunContext


def submit_tool(name: str, schema: type[LLMModel], key: str) -> BaseTool:
    """Build a strict tool that validates its input against ``schema`` and records it."""

    class SubmitTool(BaseTool):
        mutating: ClassVar[bool] = False
        parallel_safe: ClassVar[bool] = False

        async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
            try:
                obj = schema.model_validate(kwargs)
            except ValidationError as e:
                return ToolResult(content=f"invalid {schema.__name__}: {e}"[:2000], is_error=True)
            ctx.submitted[key] = obj
            return ToolResult(content="recorded")

    SubmitTool.name = name
    SubmitTool.description = (
        f"Submit your final {schema.__name__}. Call exactly once, when you are done."
    )
    SubmitTool.input_schema = schema.model_json_schema()
    SubmitTool.__name__ = f"Submit_{schema.__name__}"
    return SubmitTool()
