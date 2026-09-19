"""Spans, and where they go.

A run is a tree — phases inside a run, steps inside a phase, model calls and tool calls
inside a step — and that shape is most of what makes a trace worth having. Reading it
answers the question logs are worst at: *where did the forty minutes go*. A `run` span with
`phase.code` taking thirty of them, one `step.coder` inside it, and forty `tool.bash`
children says immediately what a thousand log lines do not.

## Export is off unless something asks for it

No `OTEL_EXPORTER_OTLP_ENDPOINT`, no exporter, and `trace_span` becomes a few dictionary
writes against a no-op span. That matters because the spans are created unconditionally at
every call site: a developer running the CLI has no collector, and paying for one would be
a reason to take the instrumentation back out.

## Langfuse is a URL, not a dependency

The phase document offers two routes — the Langfuse SDK, or OTLP pointed at Langfuse's own
OTLP endpoint. This build takes the second. Langfuse speaks OTLP natively, so an
`llm_call` span with its model, tokens and cost lands as a generation with no second client
to configure, no second set of credentials in the process, and nothing to keep in step when
either side changes. See `docs/observability.md` for the two environment variables.

The cost is that Langfuse-specific niceties (prompt management, scores) are not available
through OTLP. Nothing here uses them.

## `BatchSpanProcessor`, except in tests

Batching is what makes export affordable: spans queue and flush on a background thread
rather than blocking the run on a network call each. A test passing its own exporter gets
`SimpleSpanProcessor` instead, because a test that has to wait for a flush interval to see
its own span is a test that will be flaky.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)

from observability.logging import get_logger

log = get_logger(__name__)

TRACER_NAME = "autoswe"
ENDPOINT_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"


def _otlp_exporter() -> SpanExporter | None:
    """The configured OTLP exporter, or None when nothing is configured.

    Imported here rather than at module scope so the exporter package stays optional at
    import time: the CLI and the tests load this module and never export anything.
    """
    if not os.environ.get(ENDPOINT_VAR, "").strip():
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:  # pragma: no cover - the extra is not installed
        log.warning("otlp_exporter_unavailable", note="install opentelemetry-exporter-otlp")
        return None
    # The SDK reads the endpoint, headers and protocol from the environment itself, which
    # is also how a Langfuse endpoint and its Authorization header arrive.
    return OTLPSpanExporter()


def configure_tracing(
    service_name: str = "autoswe", exporter: SpanExporter | None = None
) -> TracerProvider:
    """Install a tracer provider for this process.

    An explicit `exporter` is the test path and is processed one span at a time; anything
    configured through the environment is batched, since a run makes hundreds of spans and
    a network round trip per span would show up in the wall clock the trace is measuring.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif (configured := _otlp_exporter()) is not None:
        provider.add_span_processor(BatchSpanProcessor(configured))
        log.info("tracing_exporting", endpoint=os.environ[ENDPOINT_VAR], service=service_name)
    trace.set_tracer_provider(provider)
    return provider


def _attr(value: Any) -> str | int | float | bool:
    return value if isinstance(value, str | int | float | bool) else str(value)


@contextmanager
def trace_span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    """A span with attributes, skipping the ones that are None.

    `None` is dropped rather than rendered as `"None"`: an absent attribute and one whose
    value is the string `None` look the same in every trace UI, and the second is a lie.
    """
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name) as span:
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, _attr(value))
        yield span


def annotate(**attrs: Any) -> None:
    """Add attributes to whatever span is currently open, if any.

    For facts that are only known once the work is done — how many tokens a call used, what
    exit code a command returned. Silent when nothing is recording, so a caller never has
    to ask whether tracing is on.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attrs.items():
        if value is not None:
            span.set_attribute(key, _attr(value))
