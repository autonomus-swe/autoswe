"""LLMProvider for any OpenAI-compatible chat endpoint (OpenRouter, vLLM, Ollama, Groq).

The tool loop is hand-written: request -> execute tool calls -> append ``tool`` messages
-> repeat until the model stops calling tools or ``max_iterations`` is hit. Structured
output (``parse``) forces a single function call whose schema is the target model, then
validates the arguments with pydantic and retries once with the validation error.
"""

from __future__ import annotations

import asyncio
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

# A 200 with no choices is a transient upstream hiccup, not a real answer.
EMPTY_RESPONSE_RETRIES = 3
EMPTY_RESPONSE_BACKOFF_S = 2.0
# How many times to remind an agent that stopped without calling its required tool.
MISSING_SUBMIT_REMINDERS = 2
# Some gateways (Groq) judge the model's generation server-side and answer 400 instead of
# handing the unusable output back: a tool call that fails schema validation, or prose where
# a tool call was due. Both are the model erring mid-run, so they are corrected, not fatal.
INVALID_TOOL_CALL_RETRIES = 3
_RECOVERABLE_MARKERS = (
    "tool_use_failed",
    "tool call validation failed",
    "output_parse_failed",
    "could not be parsed",
)
# Free tiers meter per minute, not per run. The SDK's own retries all land inside the
# same window and fail together, so a whole agent run dies on a limit that clears in
# seconds. Wait for the window the provider names, then carry on.
RATE_LIMIT_RETRIES = 6
RATE_LIMIT_MAX_WAIT_S = 90.0
RATE_LIMIT_FALLBACK_WAIT_S = 20.0


class _InvalidToolCall(Exception):
    """The gateway rejected the model's own output before this code ever saw it."""

    def __init__(self, detail: str, generation: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.generation = generation


def _invalid_tool_call_detail(err: Any) -> tuple[str, str | None] | None:
    """``(complaint, what the model generated)`` when the gateway rejected it, else None."""
    haystack = f"{getattr(err, 'code', '') or ''} {getattr(err, 'message', '') or ''}".lower()
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        haystack += " " + str(body).lower()
    if not any(marker in haystack for marker in _RECOVERABLE_MARKERS):
        return None
    detail = getattr(err, "message", None) or str(err)
    generation = None
    inner = body.get("error") if isinstance(body, dict) else None
    if isinstance(inner, dict):
        if inner.get("message"):
            detail = str(inner["message"])
        if inner.get("failed_generation"):
            generation = str(inner["failed_generation"])[:600]
    return detail[:600], generation


def _retry_after_s(err: Any, now_s: float) -> float | None:
    """How long to wait out a rate limit, or None when this is not one.

    Providers disagree on where the hint goes: ``Retry-After`` in seconds, an
    ``X-RateLimit-Reset`` epoch (OpenRouter sends milliseconds, in the body rather than
    on the response), or nothing at all. Take whichever is there and clamp it, so a
    malformed or far-future value cannot park a run for hours.
    """
    if getattr(err, "status_code", None) != 429 and type(err).__name__ != "RateLimitError":
        return None

    def clamp(value: float) -> float:
        return max(1.0, min(value, RATE_LIMIT_MAX_WAIT_S))

    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    body = getattr(err, "body", None)
    inner = body.get("error") if isinstance(body, dict) else None
    meta = inner.get("metadata") if isinstance(inner, dict) else None
    body_headers = meta.get("headers") if isinstance(meta, dict) else None
    sources: list[Any] = [headers, body_headers if isinstance(body_headers, dict) else {}]

    for source in sources:
        raw = source.get("Retry-After") or source.get("retry-after")
        if raw is not None:
            try:
                return clamp(float(raw))
            except (TypeError, ValueError):
                pass
    for source in sources:
        raw = source.get("X-RateLimit-Reset") or source.get("x-ratelimit-reset")
        if raw is None:
            continue
        try:
            reset = float(raw)
        except (TypeError, ValueError):
            continue
        if reset > 1e11:  # milliseconds, not seconds
            reset /= 1000.0
        delta = reset - now_s
        # a reset already in the past means the window just turned over
        return clamp(delta) if delta > 0 else 1.0
    return RATE_LIMIT_FALLBACK_WAIT_S


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


def _valid_arguments(raw: str | None) -> str:
    """A tool call's ``arguments`` as a JSON object string, always.

    Whatever the model emitted goes into the history we resend on every later turn, so a
    malformed value poisons the whole conversation: providers reject the entire request
    ("arguments must be a valid JSON object string") from that turn on and the run can
    never recover. The model already learns the call was invalid from its tool result, so
    unparsable arguments are replaced here with an empty object.
    """
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return "{}"
    return raw or "{}" if isinstance(parsed, dict) else "{}"


def _assistant_message(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": getattr(msg, "content", None)}
    calls = getattr(msg, "tool_calls", None) or []
    if calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": _valid_arguments(tc.function.arguments),
                },
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
        """One model turn, retrying responses that carry no usable content.

        A gateway can answer 200 with an empty ``choices`` list when its upstream hiccups
        (OpenRouter's free router does this). The SDK only retries HTTP failures, so
        without this an agentic run dies on a blip after minutes of real work.
        """
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

        resp = None
        rate_limited = 0
        attempt = 0
        while attempt <= EMPTY_RESPONSE_RETRIES:
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except APIError as e:
                rejected = _invalid_tool_call_detail(e)
                if rejected is not None:
                    raise _InvalidToolCall(*rejected) from e
                wait_s = _retry_after_s(e, time.time())
                if wait_s is not None and rate_limited < RATE_LIMIT_RETRIES:
                    rate_limited += 1
                    log.warning(
                        "provider_rate_limited",
                        model=self.model,
                        wait_s=round(wait_s, 1),
                        attempt=rate_limited,
                        retries=RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(wait_s)
                    continue  # the window, not the response, was the problem
                raise ProviderError(f"{type(e).__name__}: {getattr(e, 'message', e)}") from e
            if resp.choices:
                break
            if attempt < EMPTY_RESPONSE_RETRIES:
                log.warning(
                    "provider_empty_response",
                    model=self.model,
                    attempt=attempt + 1,
                    retries=EMPTY_RESPONSE_RETRIES,
                )
                await asyncio.sleep(EMPTY_RESPONSE_BACKOFF_S * (attempt + 1))
            attempt += 1  # only an empty answer counts against this budget
        if resp is None or not resp.choices:
            raise ProviderError(
                f"provider returned no choices {EMPTY_RESPONSE_RETRIES + 1} times "
                f"(model {self.model!r})"
            )
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
        called: set[str] = set()
        reminders = 0
        rejected = 0
        while turns < req.max_iterations:
            t0 = time.monotonic()
            try:
                turn = await self._complete(
                    messages=messages,
                    tools=tool_defs,
                    tool_choice="auto",
                    max_tokens=req.max_tokens,
                )
            except _InvalidToolCall as e:
                rejected += 1
                if rejected > INVALID_TOOL_CALL_RETRIES:
                    raise ProviderError(
                        f"the gateway rejected {rejected} malformed tool calls in a row; "
                        f"model {self.model!r} cannot drive these tools: {e.detail}"
                    ) from e
                log.warning(
                    "gateway_rejected_tool_call",
                    model=self.model,
                    attempt=rejected,
                    detail=e.detail[:200],
                )
                said = f"\nYou wrote: {e.generation}" if e.generation else ""
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your last message was rejected before it ran: {e.detail}"
                            f"{said}\nRespond with a tool call, using the tool's exact name "
                            "with no prefix and every required parameter. Do not describe "
                            "what you are about to do; call the tool."
                        ),
                    }
                )
                continue
            latency_ms = int((time.monotonic() - t0) * 1000)
            turns += 1
            total = total.add(turn.usage)
            await hooks.on_message(turn, turn.usage, latency_ms)
            messages.append(turn.raw_message)
            if turn.refusal or turn.finish_reason == "content_filter":
                return RunOutcome(turn.refusal or turn.content or "", turns, total, "refusal")
            if not turn.tool_calls:
                stop = "max_tokens" if turn.finish_reason == "length" else "end_turn"
                needs = req.must_call is not None and req.must_call not in called
                if needs and reminders < MISSING_SUBMIT_REMINDERS:
                    # Weaker models often do the work and then just stop talking. One
                    # reminder recovers the run instead of throwing the work away.
                    reminders += 1
                    log.info(
                        "reminding_agent_to_submit",
                        tool=req.must_call,
                        reminder=reminders,
                        turns=turns,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"You have not called `{req.must_call}` yet, so your work is "
                                f"not recorded. Call `{req.must_call}` now with your summary "
                                "to finish. Do not repeat the work you already did."
                            ),
                        }
                    )
                    continue
                return RunOutcome(turn.content or "", turns, total, stop)
            for call in turn.tool_calls:
                called.add(call.name)
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
