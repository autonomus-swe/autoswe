"""Convert our tools into OpenAI-style function declarations."""

from __future__ import annotations

from typing import Any

from tools.base import BaseTool


def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def to_openai_tool(tool: BaseTool) -> dict[str, Any]:
    return function_tool(tool.name, tool.description, tool.input_schema)
