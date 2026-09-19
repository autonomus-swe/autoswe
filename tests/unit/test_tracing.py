from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents.base import Agent
from contracts import ToolResult, Usage
from gateway.openai_compat_provider import ChatTurn, OpenAICompatProvider, ToolCallReq
from gateway.provider import NullHooks
from observability import tracing
from observability.tracing import annotate, configure_tracing, trace_span
from tests.fakes import make_ctx
from tools.base import BaseTool, RunContext

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module", autouse=True)
def exporter() -> InMemorySpanExporter:
    """`autouse` so this installs the global provider before any test in the module runs.

    OpenTelemetry refuses to replace a tracer provider once one is set, and the tests
    below call `configure_tracing` themselves. Without `autouse` a randomised order could
    let one of those win the race, leaving this exporter attached to a provider nothing
    traces through — and every assertion about finished spans failing for a reason that
    has nothing to do with what they test.
    """
    exp = InMemorySpanExporter()
    configure_tracing("autoswe-test", exporter=exp)
    return exp


def test_span_records_name_and_attributes(exporter: InMemorySpanExporter) -> None:
    exporter.clear()
    run_id = uuid.uuid4()
    with trace_span("phase.setup", run_id=run_id, attempt=2, skipped=None) as span:
        assert span.is_recording()
    (finished,) = exporter.get_finished_spans()
    assert finished.name == "phase.setup"
    assert finished.attributes is not None
    assert finished.attributes["run_id"] == str(run_id)
    assert finished.attributes["attempt"] == 2
    assert "skipped" not in finished.attributes


def test_nested_spans_share_a_trace(exporter: InMemorySpanExporter) -> None:
    exporter.clear()
    with trace_span("run"), trace_span("phase.code"):
        pass
    inner, outer = exporter.get_finished_spans()
    assert inner.context.trace_id == outer.context.trace_id
    assert inner.parent is not None and inner.parent.span_id == outer.context.span_id


# ---- annotate ----------------------------------------------------------------------------


def test_annotate_adds_to_whatever_span_is_open(exporter: InMemorySpanExporter) -> None:
    """For facts only known once the work is done — a token count, an exit code."""
    exporter.clear()

    with trace_span("sandbox.exec", command="pytest -q"):
        annotate(exit_code=0, truncated=False, timed_out=None)

    (finished,) = exporter.get_finished_spans()
    assert finished.attributes is not None
    assert finished.attributes["command"] == "pytest -q"
    assert finished.attributes["exit_code"] == 0
    assert finished.attributes["truncated"] is False
    assert "timed_out" not in finished.attributes, "None is absent, not the string 'None'"


def test_annotate_outside_a_span_does_nothing(exporter: InMemorySpanExporter) -> None:
    """Callers should never have to ask whether tracing is on."""
    exporter.clear()

    annotate(exit_code=1)

    assert exporter.get_finished_spans() == ()


def test_annotate_lands_on_the_innermost_span(exporter: InMemorySpanExporter) -> None:
    """The bug this guards: annotating after a `with` block closes puts the child's exit
    code on the parent, where it reads as the parent's."""
    exporter.clear()

    with trace_span("tool.bash"):
        with trace_span("sandbox.exec"):
            annotate(exit_code=2)
        annotate(tool_name="bash")

    inner, outer = exporter.get_finished_spans()
    assert inner.attributes is not None and outer.attributes is not None
    assert inner.attributes["exit_code"] == 2
    assert "exit_code" not in outer.attributes
    assert outer.attributes["tool_name"] == "bash"


# ---- export configuration -----------------------------------------------------------------


def test_nothing_is_exported_when_nothing_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spans are created unconditionally at every call site, so an unconfigured process
    has to pay nothing for them beyond a few dictionary writes."""
    monkeypatch.delenv(tracing.ENDPOINT_VAR, raising=False)

    assert tracing._otlp_exporter() is None


def test_an_endpoint_that_is_only_whitespace_is_not_an_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(tracing.ENDPOINT_VAR, "   ")

    assert tracing._otlp_exporter() is None


def test_an_endpoint_produces_an_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK reads the endpoint, headers and protocol from the environment itself, which
    is also how a Langfuse endpoint and its Authorization header arrive."""
    monkeypatch.setenv(tracing.ENDPOINT_VAR, "http://localhost:4318")

    assert tracing._otlp_exporter() is not None


def test_a_configured_provider_batches_rather_than_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network round trip per span would show up in the wall clock the trace measures.
    A test passing its own exporter gets the simple processor, because waiting for a flush
    interval to see your own span is how a test becomes flaky."""
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    monkeypatch.setenv(tracing.ENDPOINT_VAR, "http://localhost:4318")
    batched = tracing.configure_tracing("autoswe-batched")
    direct = tracing.configure_tracing("autoswe-direct", exporter=InMemorySpanExporter())

    assert any(
        isinstance(p, BatchSpanProcessor) for p in batched._active_span_processor._span_processors
    )
    assert any(
        isinstance(p, SimpleSpanProcessor) for p in direct._active_span_processor._span_processors
    )


# ---- the tree the call sites actually build ------------------------------------------------


async def test_a_step_contains_its_model_calls_and_tool_calls(
    exporter: InMemorySpanExporter, tmp_path: Path
) -> None:
    """The shape is the point. A flat list of spans says how long things took; a tree says
    which of them was waiting for which.

    Driven through the real agent and the real provider loop, because the nesting is a
    property of where the `with` blocks sit — and that is exactly what a unit test of
    `trace_span` on its own cannot see.
    """
    exporter.clear()

    class Probe(Agent):
        role = "coder"
        prompt_file = "coder"

    class Slow(BaseTool):
        name = "search_code"
        description = "search"
        input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
        mutating = False
        parallel_safe = True

        async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
            return ToolResult(content="found")

    class Scripted(OpenAICompatProvider):
        def __init__(self) -> None:
            super().__init__(model="test/model", api_key="k", base_url="http://x/v1")
            self.turns = [
                ChatTurn(
                    content=None,
                    tool_calls=[ToolCallReq(id="c0", name="search_code", arguments="{}")],
                    finish_reason="tool_calls",
                    usage=Usage(input_tokens=100, cache_read_tokens=900),
                    raw_message={"role": "assistant", "content": ""},
                ),
                ChatTurn(
                    content="done",
                    tool_calls=[],
                    finish_reason="stop",
                    usage=Usage(input_tokens=50),
                    raw_message={"role": "assistant", "content": "done"},
                ),
            ]

        async def _complete(self, **kwargs: Any) -> ChatTurn:
            return self.turns.pop(0)

    with trace_span("phase.code"):
        await Probe(tier="opus").run_tools(
            cast(Any, Scripted()), make_ctx(tmp_path), "go", cast(Any, NullHooks())
        )

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert {"phase.code", "step.coder", "llm_call", "tool.search_code"} <= set(by_name)

    def parent_of(name: str) -> str | None:
        span = by_name[name]
        if span.parent is None:
            return None
        return next(
            s.name
            for s in exporter.get_finished_spans()
            if s.context.span_id == span.parent.span_id
        )

    assert parent_of("step.coder") == "phase.code"
    assert parent_of("llm_call") == "step.coder"
    assert parent_of("tool.search_code") == "step.coder"


async def test_a_step_span_carries_what_it_cost(
    exporter: InMemorySpanExporter, tmp_path: Path
) -> None:
    """A trace that shows where the time went and not where the money went answers half
    the question anyone opens it for."""
    exporter.clear()

    class Probe(Agent):
        role = "planner"
        prompt_file = "planner"

    class Parsing:
        provider_name = "test"
        model = "test/model"

        def model_for(self, tier: str | None) -> str:
            return "medium/model" if tier == "sonnet" else self.model

        async def parse(self, req: Any, output: Any) -> Any:
            return output(), Usage(input_tokens=200, cache_read_tokens=800, cost_usd=0.5)

    await Probe(tier="sonnet").run_structured(cast(Any, Parsing()), "go", Usage)

    (step,) = [s for s in exporter.get_finished_spans() if s.name == "step.planner"]
    assert step.attributes is not None
    assert step.attributes["model"] == "medium/model", "the routed model, not the default"
    assert step.attributes["effort"] == "high", "a downgrade changes the tier, not the effort"
    assert step.attributes["step_cost_usd"] == 0.5
    assert step.attributes["step_cache_hit_rate"] == 0.8
