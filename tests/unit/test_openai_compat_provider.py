"""Provider loop tests with a scripted ``_complete``; the real API is never called."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import Field

from agents.submit import submit_tool
from contracts import LLMModel, TaskResult, ToolResult, Usage
from core.errors import ProviderError
from gateway import pricing
from gateway.openai_compat_provider import ChatTurn, OpenAICompatProvider, ToolCallReq, usage_from
from gateway.provider import NullHooks, Request
from gateway.routing import ANTHROPIC_MODELS, ROUTES, route_for
from tests.fakes import FakeSandbox, make_ctx, ok
from tools.registry import tools_for

pytestmark = pytest.mark.unit


def turn(
    content: str | None = None,
    calls: list[tuple[str, str, dict[str, Any] | str]] | None = None,
    finish: str | None = None,
    tokens: tuple[int, int] = (100, 10),
) -> ChatTurn:
    tool_calls = [
        ToolCallReq(
            id=cid, name=name, arguments=args if isinstance(args, str) else json.dumps(args)
        )
        for cid, name, args in (calls or [])
    ]
    return ChatTurn(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish or ("tool_calls" if tool_calls else "stop"),
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=0.001),
        raw_message={"role": "assistant", "content": content or ""},
    )


class ScriptedProvider(OpenAICompatProvider):
    def __init__(self, script: list[ChatTurn]) -> None:
        super().__init__(
            model="test/model:free", api_key="k", base_url="https://openrouter.ai/api/v1"
        )
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _complete(self, **kwargs: Any) -> ChatTurn:
        self.requests.append(copy.deepcopy(kwargs))  # the loop mutates its messages list
        if not self.script:
            raise AssertionError("script exhausted")
        return self.script.pop(0)


class RecordingHooks(NullHooks):
    def __init__(self, deny: str | None = None) -> None:
        self.events: list[str] = []
        self.deny = deny

    async def before_tool(self, name: str, input: dict[str, Any]) -> str | None:
        self.events.append(f"before:{name}")
        return self.deny

    async def after_tool(
        self, name: str, input: dict[str, Any], result: ToolResult, duration_ms: int
    ) -> ToolResult:
        self.events.append(f"after:{name}:{'err' if result.is_error else 'ok'}")
        return result

    async def on_message(self, message: Any, usage: Usage, latency_ms: int) -> None:
        self.events.append(f"message:{usage.input_tokens}")


async def test_loop_executes_tools_and_feeds_results_back(tmp_path: Path) -> None:
    sb = FakeSandbox(tmp_path, lambda cmd: ok("a.py\nb.py\n"))
    ctx = make_ctx(tmp_path, sb)
    provider = ScriptedProvider(
        [turn(calls=[("c1", "bash", {"command": "ls"})]), turn("done", tokens=(200, 5))]
    )
    hooks = RecordingHooks()
    out = await provider.run_tools(
        Request(role="coder", system="sys"), tools_for("coder"), ctx, hooks
    )
    assert out.stop_reason == "end_turn" and out.final_text == "done" and out.turns == 2
    assert out.usage.input_tokens == 300 and out.usage.output_tokens == 15
    assert hooks.events == ["message:100", "before:bash", "after:bash:ok", "message:200"]
    assert sb.commands == ["ls"]
    second = provider.requests[1]["messages"]
    assert second[0] == {"role": "system", "content": "sys"}
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "content": "a.py\nb.py"}
    assert provider.requests[0]["tools"][0]["function"]["name"] == "bash"
    assert provider.requests[0]["tool_choice"] == "auto"


async def test_denial_policy_and_bad_arguments_become_error_messages(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    provider = ScriptedProvider(
        [
            turn(calls=[("c1", "bash", {"command": "git status"})]),
            turn(calls=[("c2", "bash", "{not json")]),
            turn(calls=[("c3", "nope", {})]),
            turn("end"),
        ]
    )
    hooks = RecordingHooks()
    out = await provider.run_tools(
        Request(role="coder", system="s"), tools_for("coder"), ctx, hooks
    )
    assert out.stop_reason == "end_turn" and out.turns == 4
    tool_msgs = [
        m["content"] for r in provider.requests for m in r["messages"] if m["role"] == "tool"
    ]
    assert any("denied: git is not available" in m and "git_status" in m for m in tool_msgs)
    assert any("invalid JSON arguments" in m for m in tool_msgs)
    assert any("unknown tool 'nope'" in m for m in tool_msgs)
    assert "after:bash:err" in hooks.events


async def test_hook_denial_skips_execution(tmp_path: Path) -> None:
    sb = FakeSandbox(tmp_path)
    provider = ScriptedProvider([turn(calls=[("c1", "bash", {"command": "ls"})]), turn("end")])
    out = await provider.run_tools(
        Request(role="coder", system="s"),
        tools_for("coder"),
        make_ctx(tmp_path, sb),
        RecordingHooks(deny="approval required"),
    )
    assert sb.commands == [] and out.stop_reason == "end_turn"
    assert "denied: approval required" in provider.requests[1]["messages"][-1]["content"]


async def test_tool_exception_does_not_kill_the_run(tmp_path: Path) -> None:
    def boom(cmd: str) -> Any:
        raise RuntimeError("docker died")

    provider = ScriptedProvider([turn(calls=[("c1", "bash", {"command": "ls"})]), turn("end")])
    out = await provider.run_tools(
        Request(role="coder", system="s"),
        tools_for("coder"),
        make_ctx(tmp_path, FakeSandbox(tmp_path, boom)),
        NullHooks(),
    )
    assert out.stop_reason == "end_turn"
    assert (
        "tool error: RuntimeError: docker died" in provider.requests[1]["messages"][-1]["content"]
    )


async def test_max_iterations_and_length_and_refusal(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    looping = ScriptedProvider([turn(calls=[("c", "git_status", {})]) for _ in range(3)])
    out = await looping.run_tools(
        Request(role="coder", system="s", max_iterations=2), tools_for("coder"), ctx, NullHooks()
    )
    assert out.stop_reason == "max_iterations" and out.turns == 2

    cut = ScriptedProvider([turn("partial", finish="length")])
    assert (
        await cut.run_tools(Request(role="coder", system="s"), [], ctx, NullHooks())
    ).stop_reason == "max_tokens"

    refused = ScriptedProvider([turn("no", finish="content_filter")])
    assert (
        await refused.run_tools(Request(role="coder", system="s"), [], ctx, NullHooks())
    ).stop_reason == "refusal"


class Answer(LLMModel):
    value: int = Field(ge=0)
    note: str


async def test_parse_forces_a_tool_call_and_retries_once() -> None:
    good = turn(calls=[("c1", "submit_Answer", {"value": 3, "note": "ok"})])
    provider = ScriptedProvider(
        [turn(calls=[("c0", "submit_Answer", {"value": -1, "note": "bad"})]), good]
    )
    obj, usage = await provider.parse(Request(role="decomposer", system="s"), Answer)
    assert obj == Answer(value=3, note="ok") and usage.input_tokens == 200
    first = provider.requests[0]
    assert first["tool_choice"] == {"type": "function", "function": {"name": "submit_Answer"}}
    assert first["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    retry_msgs = provider.requests[1]["messages"]
    assert retry_msgs[-1]["role"] == "tool" and "invalid Answer" in retry_msgs[-1]["content"]


async def test_parse_accepts_plain_json_content_and_fails_after_two_attempts() -> None:
    provider = ScriptedProvider([turn(content=json.dumps({"value": 1, "note": "text"}))])
    obj, _ = await provider.parse(Request(role="decomposer", system="s"), Answer)
    assert obj.value == 1
    failing = ScriptedProvider([turn(content="garbage"), turn(content="still garbage")])
    with pytest.raises(ProviderError, match="twice"):
        await failing.parse(Request(role="decomposer", system="s"), Answer)


def test_usage_from_prefers_openrouter_cost_and_separates_cached_tokens() -> None:
    usage_obj = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=400, cache_write_tokens=0),
        model_extra={"cost": 0.0123},
    )
    u = usage_from(SimpleNamespace(usage=usage_obj), "some/model")
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.cost_usd) == (
        600,
        400,
        50,
        0.0123,
    )
    no_cost = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None, model_extra={}
        )
    )
    assert usage_from(no_cost, "claude-opus-5").cost_usd == pytest.approx(10 * 5e-6 + 5 * 25e-6)
    assert usage_from(SimpleNamespace(usage=None), "x") == Usage()


def test_pricing_table() -> None:
    u = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=100_000,
    )
    assert pricing.cost("claude-opus-5", u) == pytest.approx(5.0 + 2.5 + 0.5 + 0.625)
    assert pricing.cost("some-vendor/some-model:free", u) == 0.0  # any ":free" slug
    assert pricing.cost("unknown/model", u) == 0.0


def test_routes_cover_every_role_with_valid_tiers() -> None:
    for role in ("coder", "planner", "review", "pr_writer"):
        assert route_for(role).tier in ANTHROPIC_MODELS
    assert ROUTES["coder"].effort == "xhigh" and ROUTES["pr_writer"].tier == "sonnet"


class _FakeCompletions:
    """Returns each scripted response in turn; a response with no choices is a blip."""

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        return self.script.pop(0)


class _FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(script))


def _resp(choices: list[Any]) -> Any:
    return SimpleNamespace(choices=choices, usage=None, model="m")


def _choice(text: str) -> Any:
    return SimpleNamespace(
        message=SimpleNamespace(content=text, tool_calls=None, refusal=None, model_extra={}),
        finish_reason="stop",
    )


async def test_empty_response_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 with no choices is an upstream blip; it must not kill a run in progress."""
    monkeypatch.setattr("gateway.openai_compat_provider.EMPTY_RESPONSE_BACKOFF_S", 0)
    client = _FakeClient([_resp([]), _resp([]), _resp([_choice("recovered")])])
    provider = OpenAICompatProvider(
        model="flaky/model", api_key="k", base_url="http://x", client=client
    )
    turn = await provider._complete(messages=[], tools=[], tool_choice=None, max_tokens=10)
    assert turn.content == "recovered"
    assert client.chat.completions.calls == 3


async def test_empty_response_gives_up_after_the_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gateway.openai_compat_provider.EMPTY_RESPONSE_BACKOFF_S", 0)
    from gateway.openai_compat_provider import EMPTY_RESPONSE_RETRIES

    attempts = EMPTY_RESPONSE_RETRIES + 1
    client = _FakeClient([_resp([]) for _ in range(attempts)])
    provider = OpenAICompatProvider(
        model="flaky/model", api_key="k", base_url="http://x", client=client
    )
    with pytest.raises(ProviderError, match="no choices"):
        await provider._complete(messages=[], tools=[], tool_choice=None, max_tokens=10)
    assert client.chat.completions.calls == attempts


async def test_agent_is_reminded_when_it_stops_without_its_required_tool(tmp_path: Path) -> None:
    """A model that does the work and then just stops must not lose the run."""
    provider = ScriptedProvider(
        [
            turn(calls=[("c1", "git_status", {})]),
            turn("I implemented it."),  # stops without submitting
            turn(calls=[("c2", "submit_result", {"ok": True})]),
            turn("submitted"),
        ]
    )
    tools = [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")]
    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result"),
        tools,
        make_ctx(tmp_path),
        NullHooks(),
    )
    assert out.stop_reason == "end_turn" and out.turns == 4
    nudge = provider.requests[2]["messages"][-1]
    assert nudge["role"] == "user" and "submit_result" in nudge["content"]
    assert "not recorded" in nudge["content"]


async def test_reminders_are_bounded(tmp_path: Path) -> None:
    from gateway.openai_compat_provider import MISSING_SUBMIT_REMINDERS

    attempts = MISSING_SUBMIT_REMINDERS + 1
    provider = ScriptedProvider([turn("still not submitting") for _ in range(attempts)])
    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result"),
        tools_for("coder"),
        make_ctx(tmp_path),
        NullHooks(),
    )
    assert out.stop_reason == "end_turn" and out.turns == attempts


async def test_no_reminder_when_the_tool_was_already_called(tmp_path: Path) -> None:
    provider = ScriptedProvider([turn(calls=[("c1", "submit_result", {"ok": True})]), turn("done")])
    tools = [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")]
    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result"),
        tools,
        make_ctx(tmp_path),
        NullHooks(),
    )
    assert out.turns == 2  # no extra round trip
    assert all(
        m["role"] != "user" or "not recorded" not in m.get("content", "")
        for r in provider.requests
        for m in r["messages"]
    )


class RejectingProvider(OpenAICompatProvider):
    """Rejects the first `n` calls the way a server-side tool validator does."""

    def __init__(self, rejections: int, script: list[ChatTurn]) -> None:
        super().__init__(model="strict/gateway", api_key="k", base_url="http://x")
        self.left = rejections
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def _complete(self, **kwargs: Any) -> ChatTurn:
        import copy

        from gateway.openai_compat_provider import _InvalidToolCall

        self.requests.append(copy.deepcopy(kwargs))
        if self.left > 0:
            self.left -= 1
            raise _InvalidToolCall(
                "parameters for tool X did not match schema: missing 'command'",
                "Now I will edit the file.",
            )
        return self.script.pop(0)


async def test_gateway_rejected_tool_call_is_corrected_not_fatal(tmp_path: Path) -> None:
    """Groq answers 400 for a malformed tool call; the model gets told, the run continues."""
    provider = RejectingProvider(2, [turn("recovered")])
    out = await provider.run_tools(
        Request(role="coder", system="s"), tools_for("coder"), make_ctx(tmp_path), NullHooks()
    )
    assert out.stop_reason == "end_turn" and out.final_text == "recovered"
    correction = provider.requests[-1]["messages"][-1]
    assert correction["role"] == "user"
    assert "rejected before it ran" in correction["content"]
    assert "no prefix" in correction["content"]  # the model had hallucinated a namespace
    assert "You wrote: Now I will edit the file." in correction["content"]


async def test_persistent_rejection_gives_up_with_a_clear_message(tmp_path: Path) -> None:
    from gateway.openai_compat_provider import INVALID_TOOL_CALL_RETRIES

    provider = RejectingProvider(INVALID_TOOL_CALL_RETRIES + 1, [turn("never reached")])
    with pytest.raises(ProviderError, match="cannot drive these tools"):
        await provider.run_tools(
            Request(role="coder", system="s"), tools_for("coder"), make_ctx(tmp_path), NullHooks()
        )


def test_recoverable_generation_errors_are_told_apart_from_real_ones() -> None:
    from gateway.openai_compat_provider import _invalid_tool_call_detail

    bad_schema = SimpleNamespace(
        code="tool_use_failed",
        message="Tool call validation failed: missing properties: 'command'",
        body={"error": {"message": "missing properties: 'command'", "code": "tool_use_failed"}},
    )
    assert _invalid_tool_call_detail(bad_schema) == ("missing properties: 'command'", None)

    # prose where a tool call was due: the gateway hands back what the model wrote
    prose = SimpleNamespace(
        code="output_parse_failed",
        message="Parsing failed.",
        body={
            "error": {
                "message": "The model generated output that could not be parsed.",
                "code": "output_parse_failed",
                "failed_generation": "Now need to submit result.",
            }
        },
    )
    rejected = _invalid_tool_call_detail(prose)
    assert rejected is not None
    detail, generation = rejected
    assert "could not be parsed" in detail and generation == "Now need to submit result."

    for other in (
        SimpleNamespace(code="rate_limit_exceeded", message="slow down", body=None),
        SimpleNamespace(code=None, message="model not found", body={"error": {"code": 404}}),
    ):
        assert _invalid_tool_call_detail(other) is None
