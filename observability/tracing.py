"""OpenTelemetry span helper. Without an exporter it is a no-op; Phase 5 adds OTLP/Langfuse."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

TRACER_NAME = "autoswe"


def configure_tracing(
    service_name: str = "autoswe", exporter: SpanExporter | None = None
) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def _attr(value: Any) -> str | int | float | bool:
    return value if isinstance(value, str | int | float | bool) else str(value)


@contextmanager
def trace_span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name) as span:
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, _attr(value))
        yield span
