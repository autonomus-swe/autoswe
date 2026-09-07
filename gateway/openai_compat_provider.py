"""LLMProvider for any OpenAI-compatible chat endpoint (OpenRouter, vLLM, Ollama, Groq).

The tool loop is hand-written: request -> execute tool calls -> append ``tool`` messages
-> repeat until the model stops calling tools or ``max_iterations`` is hit. Structured
output (``parse``) forces a single function call whose schema is the target model, then
validates the arguments with pydantic and retries once with the validation error.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from contracts import ToolResult, Usage
from core.errors import PolicyViolation, ProviderError
from gateway import pricing
from gateway.provider import Hooks, Request, RunOutcome
from gateway.tool_schemas import function_tool, to_openai_tool
from observability.logging import get_logger
from tools.base import BaseTool, RunContext

log = get_logger(__name__)


@dataclass
class ToolCallReq:
    id: str
    name: str
    arguments: str  # raw JSON text


@dataclass
class ChatTurn:
    """One assistant response, normalised so the loop never touches SDK types."""

    content: str | None
    tool_calls: list[ToolCallReq]
    finish_reason: str  # stop | tool_calls | length | content_filter | ...
    usage: Usage
    raw_message: dict[str, Any] = field(default_factory=dict)  # appended back verbatim
    refusal: str | None = None


def usage_from(resp: Any, model: str) -> Usage:
    """Read tokens (and OpenRouter's exact ``cost`` when present) from an SDK response."""
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()
    details = getattr(u, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0)
    cache_write = int(getattr(details, "cache_write_tokens", 0) or 0)
    prompt = int(getattr(u, "prompt_tokens", 0) or 0)
    usage = Usage(
        input_tokens=max(prompt - cached - cache_write, 0),
        output_tokens=int(getattr(u, "completion_tokens", 0) or 0),
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
    )
    extra = getattr(u, "model_extra", None) or {}
    reported = extra.get("cost") if isinstance(extra, dict) else None
    cost = float(reported) if reported is not None else pricing.cost(model, usage)
    return Usage(**{**usage.model_dump(), "cost_usd": round(cost, 6)})


def _assistant_message(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": getattr(msg, "content", None)}
    calls = getattr(msg, "tool_calls", None) or []
    if calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in calls
            if getattr(tc, "type", "function") == "function"
        ]
    if out["content"] is None and not calls:
        out["content"] = ""
    extra = getattr(msg, "model_extra", None) or {}
    if isinstance(extra, dict) and extra.get("reasoning_details"):
        out["reasoning_details"] = extra["reasoning_details"]  # reasoning models need this back
    return out


class OpenAICompatProvider:
    provider_name = "openai_compat"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str,
        client: Any | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = client
        self._extra_body: dict[str, Any] = {}
        if "openrouter.ai" in base_url:
            self._extra_body["usage"] = {"include": True}  # exact cost per call

    # ---- SDK boundary (the only method that touches ``openai``) ----------------

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            headers = {
                "HTTP-Referer": "https://github.com/autonomus-swe/autoswe",
                "X-Title": "autoswe",
            }
            self._client = AsyncOpenAI(
                api_key=self._api_key or "unset",
                base_url=self.base_url,
                max_retries=3,
                timeout=self._timeout_s,
                default_headers=headers if "openrouter.ai" in self.base_url else None,
            )
        return self._client

    async def _complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: Any,
        max_tokens: int,
    ) -> ChatTurn:
        from openai import APIError

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except APIError as e:
            raise ProviderError(f"{type(e).__name__}: {getattr(e, 'message', e)}") from e
        if not resp.choices:
            raise ProviderError("provider returned no choices")
        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCallReq(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
            if getattr(tc, "type", "function") == "function"
        ]
        return ChatTurn(
            content=msg.content,
            tool_calls=calls,
            finish_reason=str(choice.finish_reason or "stop"),
            usage=usage_from(resp, self.model),
            raw_message=_assistant_message(msg),
            refusal=getattr(msg, "refusal", None),
        )

    # ---- tool loop ------------------------------------------------------------

    async def run_tools(
        self, req: Request, tools: Sequence[BaseTool], ctx: RunContext, hooks: Hooks
    ) -> RunOutcome:
        messages: list[dict[str, Any]] = [{"role": "system", "content": req.system}, *req.messages]
        tool_defs = [to_openai_tool(t) for t in tools]
        by_name = {t.name: t for t in tools}
        total = Usage()
        turns = 0
        while turns < req.max_iterations:
            t0 = time.monotonic()
            turn = await self._complete(
                messages=messages, tools=tool_defs, tool_choice="auto", max_tokens=req.max_tokens
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            turns += 1
            total = total.add(turn.usage)
            await hooks.on_message(turn, turn.usage, latency_ms)
            messages.append(turn.raw_message)
            if turn.refusal or turn.finish_reason == "content_filter":
                return RunOutcome(turn.refusal or turn.content or "", turns, total, "refusal")
            if not turn.tool_calls:
                stop = "max_tokens" if turn.finish_reason == "length" else "end_turn"
                return RunOutcome(turn.content or "", turns, total, stop)
            for call in turn.tool_calls:
                text = await self._call_tool(call, by_name, ctx, hooks)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
        return RunOutcome("", turns, total, "max_iterations")

    async def _call_tool(
        self, call: ToolCallReq, by_name: dict[str, BaseTool], ctx: RunContext, hooks: Hooks
    ) -> str:
        try:
            args = json.loads(call.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            return f"ERROR: invalid JSON arguments for {call.name}: {e}"
        tool = by_name.get(call.name)
        if tool is None:
            return f"ERROR: unknown tool {call.name!r}; available: {sorted(by_name)}"
        reason = await hooks.before_tool(tool.name, args)
        if reason:
            res = ToolResult(content=f"denied: {reason}", is_error=True)
            res = await hooks.after_tool(tool.name, args, res, 0)
            return self._render(res)
        t0 = time.monotonic()
        try:
            res = await tool(ctx, **args)
        except PolicyViolation as e:
            res = ToolResult(content=f"denied: {e}", is_error=True)
        except Exception as e:  # a tool bug must not kill the run
            log.warning("tool_exception", tool=tool.name, error=f"{type(e).__name__}: {e}")
            res = ToolResult(content=f"tool error: {type(e).__name__}: {e}", is_error=True)
        res = await hooks.after_tool(tool.name, args, res, int((time.monotonic() - t0) * 1000))
        return self._render(res)

    @staticmethod
    def _render(res: ToolResult) -> str:
        return f"ERROR: {res.content}" if res.is_error else res.content

    # ---- structured output ------------------------------------------------------

    async def parse[T: BaseModel](self, req: Request, output: type[T]) -> tuple[T, Usage]:
        name = f"submit_{output.__name__}"
        tool = function_tool(name, f"Return the {output.__name__}.", output.model_json_schema())
        choice = {"type": "function", "function": {"name": name}}
        messages: list[dict[str, Any]] = [{"role": "system", "content": req.system}, *req.messages]
        total = Usage()
        last_error = "no structured output produced"
        for _attempt in range(2):
            turn = await self._complete(
                messages=messages, tools=[tool], tool_choice=choice, max_tokens=req.max_tokens
            )
            total = total.add(turn.usage)
            if turn.refusal or turn.finish_reason == "content_filter":
                raise ProviderError(f"refusal: {turn.refusal or turn.content}")
            raw = turn.tool_calls[0].arguments if turn.tool_calls else (turn.content or "")
            try:
                return output.model_validate_json(raw), total
            except ValidationError as e:
                last_error = str(e)[:1500]
            messages.append(turn.raw_message)
            if turn.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": turn.tool_calls[0].id,
                        "content": f"ERROR: invalid {output.__name__}: {last_error}",
                    }
                )
            else:
                messages.append(
                    {"role": "user", "content": f"Your output was not valid: {last_error}"}
                )
        raise ProviderError(f"structured output failed validation twice: {last_error}")
