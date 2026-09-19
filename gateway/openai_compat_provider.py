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
from gateway import caching, context, pricing
from gateway.provider import Hooks, Request, RunOutcome
from gateway.tool_schemas import function_tool, to_openai_tool
from observability.logging import get_logger
from observability.tracing import annotate, trace_span
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
# How many tool calls may be in flight at once. Not a throughput knob — a bound on the
# database pool. Every call ends in `after_tool`, which opens a session to write the
# ledger row, and a model that asks for twenty reads in one turn would otherwise take
# twenty connections from a pool sized for five and deadlock the step it was speeding up.
MAX_PARALLEL_TOOLS = 8
# Turns a step gets after its token budget runs out, to commit and submit. Enough for a
# cooperative model to land its work; not enough for one that ignores the instruction to
# keep going to `max_iterations`, which is what makes the budget a bound rather than a
# request.
TASK_BUDGET_GRACE_TURNS = 3
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


# Weaker models mirror the JSON Schema instead of conforming to it: a `list[str]` field
# comes back as `{"items": [{"title": "..."}]}` — the schema's own wrapper, one dict per
# level of schema. The content is right and only the container is wrong, and saying so in
# a retry does not help because the model repeats the same shape. Both repairs below are
# narrow enough to be unambiguous: a dict holding nothing but the list, and a dict holding
# nothing but the string. Anything else is left alone and still fails.
REPAIR_PASSES = 3


def _at(data: Any, loc: tuple[Any, ...]) -> tuple[Any, Any] | None:
    """``(container, key)`` for a pydantic error location, or None if it does not resolve."""
    if not loc:
        return None
    node = data
    for key in loc[:-1]:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return None
    return node, loc[-1]


def _repair_once(data: Any, errors: Sequence[Any]) -> bool:
    """Apply the known structural fixes in place. True when something changed."""
    changed = False
    for err in errors:
        spot = _at(data, tuple(err.get("loc") or ()))
        if spot is None:
            continue
        container, key = spot
        try:
            value = container[key]
        except (KeyError, IndexError, TypeError):
            continue
        fixed: Any = None
        if err.get("type") == "list_type" and isinstance(value, dict) and len(value) == 1:
            inner = value.get("items")
            if isinstance(inner, list):
                fixed = inner
        elif err.get("type") == "string_type" and isinstance(value, dict) and len(value) == 1:
            only = next(iter(value.values()))
            if isinstance(only, str):
                fixed = only
        if fixed is not None:
            container[key] = fixed
            changed = True
    return changed


def repair_structured[T: BaseModel](raw: str, output: type[T]) -> T | None:
    """Validate ``raw`` after unwrapping schema-shaped containers, or None if it still fails."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for _ in range(REPAIR_PASSES):
        try:
            return output.model_validate(data)
        except ValidationError as e:
            if not _repair_once(data, e.errors()):
                return None
    return None


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


def usage_from(resp: Any, model: str, base_url: str | None = None) -> Usage:
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
    cost = float(reported) if reported is not None else pricing.cost(model, usage, base_url)
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
        models: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        # Tier -> model id, for deployments that have more than one model to offer. Empty
        # is the normal case and means every tier resolves to `model`, which is exactly
        # what every call did before budget-aware routing existed.
        self.models = {tier: name for tier, name in (models or {}).items() if name}
        self.base_url = base_url
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = client
        self._extra_body: dict[str, Any] = {}
        self._breakpoints = caching.supports_breakpoints(base_url)
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
        model: str | None = None,
    ) -> ChatTurn:
        """One model turn, retrying responses that carry no usable content.

        A gateway can answer 200 with an empty ``choices`` list when its upstream hiccups
        (OpenRouter's free router does this). The SDK only retries HTTP failures, so
        without this an agentic run dies on a blip after minutes of real work.
        """
        from openai import APIError

        # The request's model, not the provider's: after a budget downgrade they differ,
        # and `usage_from` prices the answer against whichever one is passed here. Pricing
        # a Sonnet answer at Opus rates would make the downgrade look like it saved
        # nothing, which is the one number the whole feature is judged on.
        model = model or self.model
        kwargs: dict[str, Any] = {
            "model": model,
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
        annotate(llm_model=model, llm_max_tokens=max_tokens, llm_tools=len(tools))
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
                        model=model,
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
                    model=model,
                    attempt=attempt + 1,
                    retries=EMPTY_RESPONSE_RETRIES,
                )
                await asyncio.sleep(EMPTY_RESPONSE_BACKOFF_S * (attempt + 1))
            attempt += 1  # only an empty answer counts against this budget
        if resp is None or not resp.choices:
            raise ProviderError(
                f"provider returned no choices {EMPTY_RESPONSE_RETRIES + 1} times (model {model!r})"
            )
        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCallReq(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
            if getattr(tc, "type", "function") == "function"
        ]
        usage = usage_from(resp, model, self.base_url)
        # On the span rather than only in the ledger: a trace that shows where the time
        # went and not where the money went answers half the question anyone opens it for.
        annotate(
            llm_input_tokens=usage.input_tokens,
            llm_output_tokens=usage.output_tokens,
            llm_cache_read_tokens=usage.cache_read_tokens,
            llm_cache_hit_rate=round(usage.cache_hit_rate, 3),
            llm_cost_usd=usage.cost_usd,
            llm_finish_reason=str(choice.finish_reason or "stop"),
        )
        return ChatTurn(
            content=msg.content,
            tool_calls=calls,
            finish_reason=str(choice.finish_reason or "stop"),
            usage=usage,
            raw_message=_assistant_message(msg),
            refusal=getattr(msg, "refusal", None),
        )

    def model_for(self, tier: str | None) -> str:
        """The model a tier resolves to. The default whenever this deployment has not been
        given a model for it — a single-model endpoint is the common case, and failing a
        run because a router asked for `sonnet` would be absurd."""
        return self.models.get(tier or "", self.model)

    # ---- the cached prefix ------------------------------------------------------

    def _system(self, req: Request) -> dict[str, Any]:
        """The system message: role prompt then run block, marked cacheable where that is
        read. See `gateway/caching` for why the split is where it is."""
        return {
            "role": "system",
            "content": caching.build_system(
                req.system, req.run_block, breakpoints=self._breakpoints
            ),
        }

    # ---- tool loop ------------------------------------------------------------

    async def run_tools(
        self, req: Request, tools: Sequence[BaseTool], ctx: RunContext, hooks: Hooks
    ) -> RunOutcome:
        messages: list[dict[str, Any]] = [self._system(req), *req.messages]
        tool_defs = [to_openai_tool(t) for t in tools]
        by_name = {t.name: t for t in tools}
        total = Usage()
        turns = 0
        called: set[str] = set()
        reminders = 0
        rejected = 0
        # The turn on which the task budget ran out, so it is said once rather than every
        # turn afterwards.
        over_budget: int | None = None
        # A reminder has to buy a turn, not spend the last one. Sharing the budget means a
        # model that explores to the cap gets told to submit and then has no turn left to
        # do it in, which is the same as never reminding it. Still bounded: the ceiling is
        # max_iterations + MISSING_SUBMIT_REMINDERS.
        while turns < req.max_iterations + reminders:
            t0 = time.monotonic()
            try:
                with trace_span("llm_call", role=req.role, tier=req.tier):
                    turn = await self._complete(
                        messages=messages,
                        tools=tool_defs,
                        tool_choice="auto",
                        max_tokens=req.max_tokens,
                        model=self.model_for(req.tier),
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
            for call, text in zip(
                turn.tool_calls,
                await self._run_calls(turn.tool_calls, by_name, ctx, hooks),
                strict=True,
            ):
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

            # Order matters here. Trimming rewrites old messages, which moves the cached
            # prefix, so the breakpoints are placed afterwards against the transcript that
            # will actually be sent rather than the one that existed a moment ago.
            context.trim(messages)
            if self._breakpoints:
                # A forty-turn Coder loop grows a transcript far larger than the system
                # prefix, and the two static blocks cache none of it.
                caching.moving_breakpoints(messages, turns)

            if over_budget is not None and turns - over_budget >= TASK_BUDGET_GRACE_TURNS:
                # The allowance bought a few turns to land the work. A model that spent
                # them on something else does not get more: without this the budget only
                # *asks*, and a loop that ignores it runs to `max_iterations` anyway.
                log.info(
                    "task_budget_stopped",
                    role=req.role,
                    tokens=total.total_tokens,
                    grace_turns=TASK_BUDGET_GRACE_TURNS,
                )
                return RunOutcome(turn.content or "", turns, total, "task_budget")
            if over_budget is None and _over_task_budget(req, total):
                # Not an abort. The step has spent its allowance, so it is told to land
                # what it has — the work so far is usually most of the value, and throwing
                # it away to save the last few thousand tokens is the wrong trade.
                over_budget = turns
                log.info(
                    "task_budget_spent",
                    role=req.role,
                    budget=req.task_budget_tokens,
                    tokens=total.total_tokens,
                )
                messages.append({"role": "user", "content": _land_it(req)})
        return RunOutcome("", turns, total, "max_iterations")

    @staticmethod
    def _parallelisable(calls: list[ToolCallReq], by_name: dict[str, BaseTool]) -> bool:
        """Whether this batch may run at once.

        Every call has to qualify, not just most: one that does not makes the whole batch
        serial. Three conditions, and each rules out a different way concurrency goes
        wrong.

        **`parallel_safe` and not `mutating`** — two edits to the same file racing is the
        obvious one, but `str_replace` also reads `ctx.view_hashes` to decide whether the
        file moved under it, and that check means nothing if another call is writing the
        file while it runs.

        **Not `requires_approval`** — approval is a question put to a human through a
        channel keyed by a per-call counter. Two of them in flight at once is a race for
        one answer.

        **Known to the registry** — an unknown name is the model inventing a tool, and
        nothing about an invented tool can be assumed. It falls to the serial path, where
        it turns into an error result like any other.
        """
        if len(calls) < 2:
            return False
        tools = [by_name.get(c.name) for c in calls]
        return all(
            t is not None and t.parallel_safe and not t.mutating and not t.requires_approval
            for t in tools
        )

    async def _run_calls(
        self, calls: list[ToolCallReq], by_name: dict[str, BaseTool], ctx: RunContext, hooks: Hooks
    ) -> list[str]:
        """Every call's rendered result, in the order the model asked for them.

        Three `search_code` calls against a large tree are three independent reads, and
        running them one after another is most of a Coder turn spent waiting. When they all
        qualify they run at once — and the results are still returned in call order,
        because the transcript is what the model reads next and reordering it would
        misattribute answers to questions.

        `return_exceptions=True` is not tidiness. `before_tool` raises `RunCancelled` and
        `BudgetExhausted` as control flow, and a bare `gather` propagates the first of
        those while leaving its siblings running against a run that is over — writing
        ledger rows for a cancelled run, and logging "never retrieved" for whatever they
        raised. Settling everything first and re-raising afterwards keeps the exception
        while leaving nothing behind it.
        """
        if not self._parallelisable(calls, by_name):
            return [await self._call_tool(c, by_name, ctx, hooks) for c in calls]

        log.info("tools_in_parallel", tools=[c.name for c in calls])
        limit = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

        async def bounded(call: ToolCallReq) -> str:
            async with limit:
                return await self._call_tool(call, by_name, ctx, hooks)

        settled = await asyncio.gather(*(bounded(c) for c in calls), return_exceptions=True)
        for item in settled:
            if isinstance(item, BaseException):
                raise item
        return [str(item) for item in settled]

    async def _call_tool(
        self, call: ToolCallReq, by_name: dict[str, BaseTool], ctx: RunContext, hooks: Hooks
    ) -> str:
        with trace_span(f"tool.{call.name}"):
            return await self._call_tool_inner(call, by_name, ctx, hooks)

    async def _call_tool_inner(
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
        annotate(tool_name=tool.name)
        reason = await hooks.before_tool(tool.name, args)
        if reason:
            res = ToolResult(content=f"denied: {reason}", is_error=True, denied_by="harness")
            res = await hooks.after_tool(tool.name, args, res, 0)
            return self._render(res)
        t0 = time.monotonic()
        try:
            res = await tool(ctx, **args)
        except PolicyViolation as e:
            # Recorded as a denial rather than an error: `check_bash` and `confine` raise
            # before the sandbox is touched, so nothing ran, and the ledger has to say so.
            res = ToolResult(content=f"denied: {e}", is_error=True, denied_by="policy")
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
        messages: list[dict[str, Any]] = [self._system(req), *req.messages]
        total = Usage()
        last_error = "no structured output produced"
        for _attempt in range(2):
            with trace_span("llm_call", role=req.role, tier=req.tier, structured=output.__name__):
                turn = await self._complete(
                    messages=messages,
                    tools=[tool],
                    tool_choice=choice,
                    max_tokens=req.max_tokens,
                    model=self.model_for(req.tier),
                )
            total = total.add(turn.usage)
            if turn.refusal or turn.finish_reason == "content_filter":
                raise ProviderError(f"refusal: {turn.refusal or turn.content}")
            raw = turn.tool_calls[0].arguments if turn.tool_calls else (turn.content or "")
            try:
                return output.model_validate_json(raw), total
            except ValidationError as e:
                last_error = str(e)[:1500]
                repaired = repair_structured(raw, output)
                if repaired is not None:
                    log.info("structured_output_repaired", model=self.model, output=output.__name__)
                    return repaired, total
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


def _over_task_budget(req: Request, spent: Usage) -> bool:
    """Whether this step has spent the allowance for its role."""
    budget = req.task_budget_tokens
    return budget is not None and spent.total_tokens >= budget


def _land_it(req: Request) -> str:
    """What a step is told when its budget runs out.

    Deliberately not "stop". A Coder forty turns into a task has usually done most of the
    work, and discarding it to save the last few thousand tokens is the wrong trade — so it
    is asked to commit what is consistent and submit, which is the same thing the run-level
    budget gate asks for.
    """
    finish = f" then call `{req.must_call}`" if req.must_call else ""
    return (
        "You have used this step's token budget. Do not start anything new. Commit whatever "
        f"is already consistent with `git_commit`,{finish} and say plainly in the summary "
        "what is unfinished."
    )
