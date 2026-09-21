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
from contracts.plan import TaskGraphSpec, TaskSpec
from core.errors import ProviderError
from gateway import caching, pricing
from gateway import context as gateway_context
from gateway.openai_compat_provider import (
    RATE_LIMIT_FALLBACK_WAIT_S,
    RATE_LIMIT_MAX_WAIT_S,
    ChatTurn,
    OpenAICompatProvider,
    ToolCallReq,
    _retry_after_s,
    repair_structured,
    usage_from,
)
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
    # `raw_message` carries the tool calls, as `_assistant_message` builds it from a real
    # response. Without them the transcript this fake produces has no record of what was
    # called, and anything that reads the history back — context trimming, a test of it —
    # is measuring the fake rather than the loop.
    raw: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        raw["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
            for c in tool_calls
        ]
    return ChatTurn(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish or ("tool_calls" if tool_calls else "stop"),
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=0.001),
        raw_message=raw,
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


async def test_the_first_reminder_asks_and_the_last_one_compels(tmp_path: Path) -> None:
    """Asking twice and giving up threw away a real ANALYZE step.

    qwen2.5:7b declined `submit_profile` through both reminders and lost four turns of
    correct work. A model that has stopped without submitting as many times as we are
    willing to ask has demonstrated that asking does not work — so the last reminder names
    the tool in `tool_choice` rather than in prose.

    The asymmetry is deliberate. The first reminder stays a request, because a model that
    genuinely has one more thing to look at should be allowed to; by the last one, the run
    is about to be discarded over a tool call rather than over the work.
    """
    provider = ScriptedProvider(
        [
            turn("I have analyzed it."),  # stops without submitting
            turn("Yes, it is analyzed."),  # ignores the polite reminder
            turn(calls=[("c1", "submit_result", {"ok": True})]),
            turn("done"),
        ]
    )
    tools = [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")]

    await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result"),
        tools,
        make_ctx(tmp_path),
        NullHooks(),
    )

    choices = [r["tool_choice"] for r in provider.requests]
    assert choices[0] == "auto", "the model gets to decide until it has shown it will not"
    assert choices[1] == "auto", "the first reminder is still a request"
    assert choices[2] == {"type": "function", "function": {"name": "submit_result"}}


async def test_the_forced_choice_names_the_tool_rather_than_asking_for_any_tool() -> None:
    """`"required"` would be satisfied by the agent's next `read_file`, which is exactly the
    behaviour being corrected — a model looping happily on tools while never submitting."""
    from gateway.openai_compat_provider import _forced_choice

    assert _forced_choice("submit_profile") == {
        "type": "function",
        "function": {"name": "submit_profile"},
    }


async def test_an_endpoint_that_refuses_a_forced_choice_still_gets_its_turn(
    tmp_path: Path,
) -> None:
    """Every gateway measured here takes a named `tool_choice`, but that is a fact about the
    ones measured. Losing a whole run to an unsupported parameter would be worse than the
    reminder this replaces, so the turn is retried the old way."""

    class Picky(ScriptedProvider):
        def __init__(self, script: list[ChatTurn]) -> None:
            super().__init__(script)
            self.refusals = 0

        async def _complete(self, **kwargs: Any) -> ChatTurn:
            if kwargs.get("tool_choice") not in ("auto", None):
                self.refusals += 1
                raise ProviderError("BadRequestError: tool_choice is not supported")
            return await super()._complete(**kwargs)

    provider = Picky(
        [
            turn("done thinking"),
            turn("still not submitting"),
            turn(calls=[("c1", "submit_result", {"ok": True})]),
            turn("ok"),
        ]
    )
    tools = [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")]

    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result"),
        tools,
        make_ctx(tmp_path),
        NullHooks(),
    )

    assert provider.refusals == 1, "it should try once and not keep trying"
    assert out.stop_reason == "end_turn"
    assert all(r["tool_choice"] == "auto" for r in provider.requests)


async def test_a_provider_error_on_an_unforced_turn_is_still_fatal(tmp_path: Path) -> None:
    """The control for the fallback above. Swallowing every `ProviderError` would turn a
    dead endpoint into an infinite loop that looks like a slow run."""

    class Broken(ScriptedProvider):
        async def _complete(self, **kwargs: Any) -> ChatTurn:
            raise ProviderError("APIConnectionError: the endpoint is gone")

    with pytest.raises(ProviderError, match="endpoint is gone"):
        await Broken([]).run_tools(
            Request(role="coder", system="s", must_call="submit_result"),
            tools_for("coder"),
            make_ctx(tmp_path),
            NullHooks(),
        )


async def test_a_reminder_buys_a_turn_rather_than_spending_the_last_one(tmp_path: Path) -> None:
    """A model that explores right up to the cap must still get a turn to submit in.

    Sharing one budget meant the reminder landed on the final iteration and the run died
    at max_iterations holding work it had already done — seen for real on the analyzer,
    which explored for eleven turns of twelve and was reminded on the eleventh.
    """
    tools = [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")]
    explored = [turn(calls=[("c1", "git_status", {})]) for _ in range(2)]
    submission = turn(
        calls=[
            (
                "c9",
                "submit_result",
                {
                    "summary": "done at last",
                    "files_touched": ["a.py"],
                    "how_to_test": "pytest",
                    "notes_for_reviewer": [],
                },
            )
        ]
    )
    provider = ScriptedProvider([*explored, turn("I am finished"), submission, turn("ok")])
    ctx = make_ctx(tmp_path)
    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result", max_iterations=3),
        tools,
        ctx,
        NullHooks(),
    )
    # three iterations were allowed; the reminder on the third earns a fourth to act in
    assert out.turns == 4, out.stop_reason
    nudge = provider.requests[3]["messages"][-1]
    assert nudge["role"] == "user" and "submit_result" in nudge["content"]
    # what the agents actually check is the submission, not how the loop stopped
    assert isinstance(ctx.submitted.get("task_result"), TaskResult)


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"command": "ls"}', '{"command": "ls"}'),  # valid object: passed through byte for byte
        (None, "{}"),
        ("", "{}"),
        ('{"path": "a.py", "content": "def f(:', "{}"),  # truncated mid-string
        ('{"a": 1', "{}"),  # unclosed brace
        ("not json at all", "{}"),
        ("[1, 2, 3]", "{}"),  # valid JSON but not an object
        ('"a string"', "{}"),
    ],
)
def test_tool_call_arguments_in_history_are_always_valid_json_objects(
    raw: str | None, expected: str
) -> None:
    """Malformed arguments must never enter the history we resend.

    Whatever the model emits is echoed on every later turn, so one bad value made every
    subsequent request rejected with "arguments must be a valid JSON object string" and
    the run could not recover.
    """
    from gateway.openai_compat_provider import _valid_arguments

    assert _valid_arguments(raw) == expected
    json.loads(_valid_arguments(raw))  # always parseable


def test_assistant_message_sanitises_a_malformed_tool_call() -> None:
    from gateway.openai_compat_provider import _assistant_message

    msg = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="c1",
                type="function",
                function=SimpleNamespace(name="run_tests", arguments='{"selector": "tests/'),
            )
        ],
        model_extra={},
    )
    echoed = _assistant_message(msg)
    assert echoed["tool_calls"][0]["function"]["arguments"] == "{}"
    assert echoed["tool_calls"][0]["function"]["name"] == "run_tests"
    assert echoed["tool_calls"][0]["id"] == "c1"


# ---- rate limits -------------------------------------------------------------------
# A free tier meters per minute, not per run. The wait has to be read from whichever
# place the provider chose to put it, or an agent run dies on a limit clearing in seconds.


def _limit_err(*, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    """Stands in for openai.RateLimitError, which is matched by name and status."""
    kind = type("RateLimitError", (SimpleNamespace,), {})
    return kind(status_code=429, body=body, response=SimpleNamespace(headers=headers or {}))


def test_retry_after_ignores_anything_that_is_not_a_rate_limit() -> None:
    assert _retry_after_s(SimpleNamespace(status_code=500, body=None), 1000.0) is None
    assert _retry_after_s(SimpleNamespace(status_code=400, body={"error": {}}), 1000.0) is None


def test_retry_after_reads_the_body_the_openrouter_free_tier_actually_sends() -> None:
    """The reset is epoch milliseconds, nested in the body, not on the response."""
    err = _limit_err(
        body={
            "error": {
                "message": "Rate limit exceeded: free-models-per-min. ",
                "code": 429,
                "metadata": {
                    "headers": {
                        "X-RateLimit-Limit": "20",
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "1789203360000",
                    },
                    "limit_source": "openrouter_free_tier_per_minute",
                },
            }
        }
    )
    assert _retry_after_s(err, 1789203335.0) == pytest.approx(25.0)


def test_retry_after_prefers_a_plain_retry_after_header() -> None:
    err = _limit_err(
        headers={"Retry-After": "7"},
        body={"error": {"metadata": {"headers": {"X-RateLimit-Reset": "1789203360000"}}}},
    )
    assert _retry_after_s(err, 0.0) == pytest.approx(7.0)


def test_retry_after_clamps_a_hostile_or_stale_reset() -> None:
    far = _limit_err(
        body={"error": {"metadata": {"headers": {"X-RateLimit-Reset": "99999999999"}}}}
    )
    assert _retry_after_s(far, 0.0) == RATE_LIMIT_MAX_WAIT_S, "never park a run for hours"
    past = _limit_err(body={"error": {"metadata": {"headers": {"X-RateLimit-Reset": "10"}}}})
    assert _retry_after_s(past, 1000.0) == 1.0, "the window already turned over"
    assert _retry_after_s(_limit_err(headers={"Retry-After": "soon"}), 0.0) == (
        RATE_LIMIT_FALLBACK_WAIT_S
    )


def test_retry_after_falls_back_when_the_provider_says_nothing() -> None:
    assert _retry_after_s(_limit_err(), 0.0) == RATE_LIMIT_FALLBACK_WAIT_S


# ---- structured-output repair -------------------------------------------------------
# qwen2.5:7b decomposed the Phase 2 goal into seven correct tasks and then wrapped every
# list field in the schema's own container. The payloads below are what it actually sent.


def test_repair_unwraps_the_schema_shaped_container_a_local_model_sends() -> None:
    raw = json.dumps(
        {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Implement subtract",
                    "description": "Add subtract(a, b) to fixture/ops.py",
                    "depends_on": {"items": []},
                    "files": {"items": [{"title": "fixture/ops.py"}]},
                    "acceptance_criteria": {"items": [{"title": "subtract(3, 2) returns 1"}]},
                    "test_selector": "tests/test_ops.py",
                }
            ]
        }
    )
    graph = repair_structured(raw, TaskGraphSpec)
    assert graph is not None, "the content was right; only the container was wrong"
    task = graph.tasks[0]
    assert task.acceptance_criteria == ["subtract(3, 2) returns 1"]
    assert task.files == ["fixture/ops.py"]
    assert task.depends_on == []
    assert task.id == "t1" and task.test_selector == "tests/test_ops.py"


def test_repair_wraps_the_one_item_answer_a_model_wrote_as_the_item() -> None:
    """The payload below killed a real DECOMPOSE step, twice in a row.

    Asked for `acceptance_criteria: list[str]`, qwen2.5:7b answered with the criterion —
    because that is what the question sounds like. Retrying does not help: the phrasing
    that produced it is the phrasing that will produce it again.
    """
    raw = json.dumps(
        {
            "tasks": [
                {
                    **VALID_TASK,
                    "acceptance_criteria": "fixture/ops.py contains `def subtract(a, b): "
                    "return a - b`.;",
                }
            ]
        }
    )

    graph = repair_structured(raw, TaskGraphSpec)

    assert graph is not None
    assert graph.tasks[0].acceptance_criteria == [
        "fixture/ops.py contains `def subtract(a, b): return a - b`.;"
    ], "the semicolon is inside a code snippet; splitting on it invents a second criterion"


def test_repair_will_not_turn_a_null_into_a_list_holding_nothing() -> None:
    """`None` means the model had nothing to say. `[None]` is a list with one empty
    criterion in it, which reads downstream as a requirement nobody can satisfy.

    The null is dropped rather than wrapped, so the field falls back to its default — and
    `acceptance_criteria` has none, so this still fails, which is the right answer. A task
    with no acceptance criteria is not a task.
    """
    raw = json.dumps({"tasks": [{**VALID_TASK, "acceptance_criteria": None}]})

    assert repair_structured(raw, TaskGraphSpec) is None


def test_a_null_falls_back_to_the_default_the_schema_already_declares() -> None:
    """The other half, and the one that killed a real run.

    An honest "I have no test selector for this task" came back as `null`, and pydantic
    does not apply a default to a key that is present and null — only to a missing one. So
    the Decomposer's most truthful answer was the one that failed.

    Nothing is invented here: the value used is the one the schema author wrote.
    """
    raw = json.dumps({"tasks": [{**VALID_TASK, "test_selector": None}]})

    graph = repair_structured(raw, TaskGraphSpec)

    assert graph is not None
    assert graph.tasks[0].test_selector == "", "empty means the full suite, everywhere else"


def test_an_empty_selector_means_the_full_suite_rather_than_a_missing_value() -> None:
    """The semantics the default rests on, asserted so the default cannot quietly become a
    different kind of empty. Four call sites construct a TaskSpec this way."""
    task = TaskGraphSpec.model_validate({"tasks": [VALID_TASK]}).tasks[0]

    assert TaskSpec(**{**VALID_TASK, "test_selector": ""}).test_selector == ""
    assert task.test_selector == VALID_TASK["test_selector"]


def test_repair_leaves_a_valid_payload_untouched() -> None:
    raw = json.dumps({"tasks": [dict(VALID_TASK)]})
    assert repair_structured(raw, TaskGraphSpec) == TaskGraphSpec.model_validate(
        {"tasks": [VALID_TASK]}
    )


def test_repair_refuses_what_it_cannot_unambiguously_fix() -> None:
    """A wrong container is recoverable. Missing or ambiguous content is not."""
    missing = json.dumps({"tasks": [{"id": "t1", "title": "no description or files"}]})
    assert repair_structured(missing, TaskGraphSpec) is None
    # two keys: which one is the list? refuse rather than guess
    ambiguous = json.dumps(
        {"tasks": [{**VALID_TASK, "files": {"items": ["a.py"], "extra": ["b.py"]}}]}
    )
    assert repair_structured(ambiguous, TaskGraphSpec) is None
    assert repair_structured("not json at all", TaskGraphSpec) is None


VALID_TASK = {
    "id": "t1",
    "title": "Implement subtract",
    "description": "Add subtract(a, b) to fixture/ops.py",
    "depends_on": [],
    "files": ["fixture/ops.py"],
    "acceptance_criteria": ["subtract(3, 2) returns 1"],
    "test_selector": "tests/test_ops.py",
}


# ---- the cached prefix -----------------------------------------------------------------
#
# What gets *sent*, as opposed to what `gateway/caching` computes. The two can drift: a
# provider that builds the blocks correctly and then rebuilds them per turn caches nothing,
# and every unit test of the builder would still pass.


LONG_PROMPT = "You are the Coder. " + "Follow the conventions you see. " * 80


async def test_the_system_message_is_the_role_prompt_then_the_run_block(tmp_path: Path) -> None:
    provider = ScriptedProvider([turn("done")])

    await provider.run_tools(
        Request(role="coder", system=LONG_PROMPT, run_block="# Repository\ntest: pytest -q"),
        tools_for("coder"),
        make_ctx(tmp_path),
        NullHooks(),
    )

    blocks = provider.requests[0]["messages"][0]["content"]
    assert [b["text"] for b in blocks] == [LONG_PROMPT, "# Repository\ntest: pytest -q"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


async def test_the_prefix_does_not_move_between_turns_of_one_loop(tmp_path: Path) -> None:
    """The failure this exists for: a prefix rebuilt per turn is a cache nothing ever
    reads, and the run looks identical apart from the bill."""
    sb = FakeSandbox(tmp_path, lambda cmd: ok("out\n"))
    provider = ScriptedProvider(
        [
            turn(calls=[("c1", "bash", {"command": "ls"})]),
            turn(calls=[("c2", "bash", {"command": "ls"})]),
            turn("done"),
        ]
    )

    await provider.run_tools(
        Request(role="coder", system=LONG_PROMPT, run_block="# Repository\nfixed"),
        tools_for("coder"),
        make_ctx(tmp_path, sb),
        NullHooks(),
    )

    prefixes = {
        caching.prefix_of(r["messages"][0]["content"], r["tools"]) for r in provider.requests
    }
    assert len(prefixes) == 1, "the cached prefix changed mid-loop"


async def test_an_endpoint_that_does_not_read_cache_control_gets_a_plain_string(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([turn("done")])
    provider.base_url = "https://api.openai.com/v1"
    provider._breakpoints = False

    await provider.run_tools(
        Request(role="coder", system=LONG_PROMPT),
        tools_for("coder"),
        make_ctx(tmp_path),
        NullHooks(),
    )

    assert provider.requests[0]["messages"][0]["content"] == LONG_PROMPT


async def test_a_long_loop_marks_a_tool_result_so_the_transcript_caches_too(
    tmp_path: Path,
) -> None:
    """Two static blocks cache the prefix; a forty-turn Coder loop grows a transcript far
    larger than that, and none of it was covered."""
    sb = FakeSandbox(tmp_path, lambda cmd: ok("out\n"))
    calls = [
        turn(calls=[(f"c{i}", "bash", {"command": "ls"})]) for i in range(caching.TOOL_RESULT_EVERY)
    ]
    provider = ScriptedProvider([*calls, turn("done")])

    await provider.run_tools(
        Request(role="coder", system=LONG_PROMPT, run_block="fixed"),
        tools_for("coder"),
        make_ctx(tmp_path, sb),
        NullHooks(),
    )

    final = provider.requests[-1]["messages"]
    assert any(caching._is_marked(m) for m in final), "no moving breakpoint was placed"
    assert sum(caching._is_marked(m) for m in final) <= caching.MOVING_BREAKPOINTS


def test_the_cache_hit_rate_is_what_the_model_read_from_cache() -> None:
    assert Usage(input_tokens=200, cache_read_tokens=800).cache_hit_rate == 0.8
    assert Usage(input_tokens=1000).cache_hit_rate == 0.0
    assert Usage().cache_hit_rate == 0.0, "no call is not a missed cache"


def test_output_tokens_do_not_count_against_the_cache_hit_rate() -> None:
    """They were never candidates for a hit, so counting them would make a long answer
    look like a caching failure."""
    read_everything = Usage(input_tokens=0, cache_read_tokens=1000, output_tokens=5000)

    assert read_everything.cache_hit_rate == 1.0


# ---- context editing and task budgets, through the loop --------------------------------------
#
# The joins. `context.trim` can be correct and the budget arithmetic can be correct, and
# the feature still does nothing if the loop never calls one or never acts on the other.


def big_call(i: int) -> ChatTurn:
    # `bash`, not `read_file`: the editor tools read the real filesystem host-side, so a
    # FakeSandbox handler never reaches them and the transcript stays small — which is how
    # the first version of this test asserted trimming against a loop that had nothing to
    # trim.
    return turn(calls=[(f"c{i}", "bash", {"command": f"cat f{i}.py"})], tokens=(5_000, 100))


async def test_a_long_loop_has_its_old_tool_results_cleared(tmp_path: Path) -> None:
    """Measured through the real loop: the transcript that goes out on the last turn is
    smaller than the one that would have, and the recent results are intact."""
    big = "x" * 12_000
    sb = FakeSandbox(tmp_path, lambda cmd: ok(big))
    script = [big_call(i) for i in range(40)] + [turn("done")]
    provider = ScriptedProvider(script)

    await provider.run_tools(
        Request(role="coder", system=LONG_PROMPT),
        tools_for("coder"),
        make_ctx(tmp_path, sb),
        NullHooks(),
    )

    final = provider.requests[-1]["messages"]
    results = [m for m in final if m.get("role") == "tool"]
    cleared = [m for m in results if gateway_context.CLEARED in str(m["content"])]
    assert cleared, "a forty-turn loop should have trimmed something"
    assert all(gateway_context.CLEARED not in str(m["content"]) for m in results[-3:]), (
        "the most recent results are what the model is working from"
    )


async def test_the_calls_survive_the_clearing(tmp_path: Path) -> None:
    """A loop that forgets what it ran runs it again, which costs more than the clearing
    saved."""
    sb = FakeSandbox(tmp_path, lambda cmd: ok("x" * 12_000))
    provider = ScriptedProvider([big_call(i) for i in range(40)] + [turn("done")])

    await provider.run_tools(
        Request(role="coder", system=LONG_PROMPT),
        tools_for("coder"),
        make_ctx(tmp_path, sb),
        NullHooks(),
    )

    final = provider.requests[-1]["messages"]
    assert sum(1 for m in final if m.get("tool_calls")) == 40


async def test_a_step_that_spends_its_budget_is_asked_to_land_the_work(tmp_path: Path) -> None:
    sb = FakeSandbox(tmp_path, lambda cmd: ok("out"))
    provider = ScriptedProvider([big_call(i) for i in range(10)] + [turn("done")])

    await provider.run_tools(
        Request(role="coder", system=LONG_PROMPT, task_budget_tokens=8_000),
        tools_for("coder"),
        make_ctx(tmp_path, sb),
        NullHooks(),
    )

    nudges = [
        m
        for r in provider.requests
        for m in r["messages"]
        if m.get("role") == "user" and "token budget" in str(m.get("content", ""))
    ]
    assert nudges, "the step was never told it had run out"
    assert "git_commit" in str(nudges[0]["content"])


async def test_a_step_that_ignores_the_nudge_is_stopped(tmp_path: Path) -> None:
    """Without this the budget only *asks*: a loop that keeps calling tools runs to
    `max_iterations` and spends the money anyway."""
    sb = FakeSandbox(tmp_path, lambda cmd: ok("out"))
    provider = ScriptedProvider([big_call(i) for i in range(40)] + [turn("done")])

    out = await provider.run_tools(
        Request(role="coder", system=LONG_PROMPT, task_budget_tokens=8_000, max_iterations=40),
        tools_for("coder"),
        make_ctx(tmp_path, sb),
        NullHooks(),
    )

    assert out.stop_reason == "task_budget"
    assert out.turns < 40, "it stopped rather than running to the iteration cap"


async def test_a_step_with_no_budget_runs_to_its_own_end(tmp_path: Path) -> None:
    """The control. Most roles have no ceiling, and must be unaffected."""
    sb = FakeSandbox(tmp_path, lambda cmd: ok("out"))
    provider = ScriptedProvider([big_call(0), turn("done")])

    out = await provider.run_tools(
        Request(role="planner", system=LONG_PROMPT),
        tools_for("coder"),
        make_ctx(tmp_path, sb),
        NullHooks(),
    )

    assert out.stop_reason == "end_turn" and out.final_text == "done"


async def test_a_model_that_explores_to_the_cap_is_still_made_to_submit(tmp_path: Path) -> None:
    """The exit a large repository takes, and the one `must_call` did not cover.

    Every reminder above fires on the path where the model *stops talking*. A model that
    keeps calling tools until its iterations run out never reaches it, so the loop just
    ended and threw the step away. Measured on Django: the Analyzer explored 3 043 files
    for twelve turns and died on `max_iterations` holding a profile it had assembled.

    The bigger the repository, the more certain that exit becomes, which is backwards.
    """
    explore = [turn(calls=[("c", "git_status", {})]) for _ in range(2)]
    submission = turn(calls=[("final", "submit_result", {"ok": True})])
    provider = ScriptedProvider([*explore, submission])
    tools = [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")]

    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result", max_iterations=2),
        tools,
        make_ctx(tmp_path),
        NullHooks(),
    )

    assert out.stop_reason == "end_turn", "the run ended at the cap without submitting"
    assert provider.requests[-1]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_result"},
    }


async def test_the_forced_turn_is_not_taken_when_the_tool_was_already_called(
    tmp_path: Path,
) -> None:
    """The control. A run that hits the cap having already submitted has nothing to force,
    and spending a call to prove it would be a cost on every long step."""
    calls = [turn(calls=[("c1", "submit_result", {"ok": True})])]
    calls += [turn(calls=[("c", "git_status", {})]) for _ in range(2)]
    provider = ScriptedProvider(calls)
    tools = [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")]

    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result", max_iterations=3),
        tools,
        make_ctx(tmp_path),
        NullHooks(),
    )

    assert out.stop_reason == "max_iterations"
    assert all(r["tool_choice"] == "auto" for r in provider.requests)


async def test_a_step_with_no_required_tool_still_just_stops_at_the_cap(tmp_path: Path) -> None:
    """Roles that make one call have no `must_call`, and must not gain an extra turn."""
    provider = ScriptedProvider([turn(calls=[("c", "git_status", {})]) for _ in range(3)])

    out = await provider.run_tools(
        Request(role="coder", system="s", max_iterations=2),
        tools_for("coder"),
        make_ctx(tmp_path),
        NullHooks(),
    )

    assert out.stop_reason == "max_iterations" and out.turns == 2


async def test_the_forced_turn_reaches_the_ledger(tmp_path: Path) -> None:
    """`on_message` writes the `llm_calls` row, moves live spend and records the metric.

    Adding the turn's usage to the loop's own `total` is not the same thing: `db.run_cost`
    reads the table, and every figure in `docs/numbers.md` is computed from it. A turn that
    never reaches `on_message` is invisible to the cost of the run.

    This mattered more than a missing row usually would. On a large repository the Analyzer
    hits `max_iterations` every time, so the forced turn is the *normal* exit at scale, and
    it carries the largest prefix of the step — the undercount would have been worst
    exactly where the measurement matters most.
    """
    explore = [turn(calls=[("c", "git_status", {})]) for _ in range(2)]
    submission = turn(calls=[("final", "submit_result", {"ok": True})], tokens=(9_000, 40))
    provider = ScriptedProvider([*explore, submission])
    tools = [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")]
    hooks = RecordingHooks()

    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result", max_iterations=2),
        tools,
        make_ctx(tmp_path),
        hooks,
    )

    assert hooks.events.count("message:9000") == 1, "the forced turn never reached on_message"
    assert out.usage.input_tokens == 9_200, "and its tokens are in the outcome too"


async def test_a_forced_turn_the_endpoint_refused_is_not_billed(tmp_path: Path) -> None:
    """The control. A call that never produced a response must not appear in the ledger,
    or the fallback would invent spend that did not happen."""

    class Picky(ScriptedProvider):
        async def _complete(self, **kwargs: Any) -> ChatTurn:
            if kwargs.get("tool_choice") not in ("auto", None):
                raise ProviderError("BadRequestError: tool_choice is not supported")
            return await super()._complete(**kwargs)

    provider = Picky([turn(calls=[("c", "git_status", {})]) for _ in range(2)])
    hooks = RecordingHooks()

    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result", max_iterations=2),
        [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")],
        make_ctx(tmp_path),
        hooks,
    )

    assert out.stop_reason == "max_iterations"
    assert hooks.events.count("message:100") == 2, "only the two turns that happened"


async def test_a_gateway_that_rejects_the_forced_call_does_not_kill_the_step(
    tmp_path: Path,
) -> None:
    """`_InvalidToolCall` is a plain `Exception`, not a `ProviderError`.

    Catching only the latter let it escape `run_tools` uncaught and end the step with an
    internal type instead of an outcome. Forcing makes it *more* likely, not less: gateways
    that judge a generation server-side reject exactly the case where a model is compelled
    to emit one specific call.
    """
    from gateway.openai_compat_provider import _InvalidToolCall

    class Judging(ScriptedProvider):
        async def _complete(self, **kwargs: Any) -> ChatTurn:
            if kwargs.get("tool_choice") not in ("auto", None):
                raise _InvalidToolCall("tool_use_failed: the model emitted prose")
            return await super()._complete(**kwargs)

    provider = Judging([turn(calls=[("c", "git_status", {})]) for _ in range(2)])

    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result", max_iterations=2),
        [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")],
        make_ctx(tmp_path),
        NullHooks(),
    )

    assert out.stop_reason == "max_iterations", "the step ended as it would have anyway"


async def test_a_refusal_on_the_forced_turn_is_reported_as_a_refusal(tmp_path: Path) -> None:
    """Reading it as "no tool calls" would file a model that declined under the same
    heading as one that ran out of turns, and those want different answers from whoever
    reads the run."""
    explore = [turn(calls=[("c", "git_status", {})]) for _ in range(2)]
    provider = ScriptedProvider([*explore, turn("I will not", finish="content_filter")])

    out = await provider.run_tools(
        Request(role="coder", system="s", must_call="submit_result", max_iterations=2),
        [*tools_for("coder"), submit_tool("submit_result", TaskResult, "task_result")],
        make_ctx(tmp_path),
        NullHooks(),
    )

    assert out.stop_reason == "refusal"
    assert out.final_text == "I will not"


def test_a_tool_call_goes_back_with_whatever_it_arrived_with() -> None:
    """Gemini 3.x attaches an opaque `extra_content.google.thought_signature` to a function
    call and rejects the *next* turn without it: "Function call is missing a
    thought_signature in functionCall parts". Turn one always worked, turn two always 400'd.

    Rebuilding a tool call from `id`/`type`/`function` and dropping the rest is what caused
    that. The rule is to carry unknown fields rather than enumerate known ones, because the
    next provider will invent a different name for the same idea — `reasoning_details` is
    the same problem one field up.
    """
    from gateway.openai_compat_provider import _tool_call_back

    signature = {"google": {"thought_signature": "EnEKbwFpFH0TYa7kKs73"}}
    call = SimpleNamespace(
        id="c1",
        type="function",
        function=SimpleNamespace(name="read_file", arguments='{"path": "a.py"}'),
        model_extra={"extra_content": signature},
    )

    back = _tool_call_back(call)

    assert back["extra_content"] == signature, "the provider's own token was dropped"
    assert back["id"] == "c1" and back["function"]["name"] == "read_file"


def test_a_provider_that_sends_no_extras_produces_a_clean_call() -> None:
    """The control. Carrying unknown fields must not invent one — a stray key here would
    be sent to every other provider, which has no idea what it is."""
    from gateway.openai_compat_provider import _tool_call_back

    call = SimpleNamespace(
        id="c1",
        type="function",
        function=SimpleNamespace(name="read_file", arguments='{"path": "a.py"}'),
        model_extra={},
    )

    assert set(_tool_call_back(call)) == {"id", "type", "function"}


def test_invalid_arguments_are_still_normalised_through_the_new_path() -> None:
    """`arguments` is not opaque: a model emitting non-JSON there is a case the loop
    repairs, so it must keep being normalised even while everything else is passed on."""
    from gateway.openai_compat_provider import _tool_call_back

    call = SimpleNamespace(
        id="c1",
        type="function",
        function=SimpleNamespace(name="read_file", arguments="{not json"),
        model_extra={},
    )

    assert _tool_call_back(call)["function"]["arguments"] == "{}"
