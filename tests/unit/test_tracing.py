from __future__ import annotations

import uuid

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from observability.tracing import configure_tracing, trace_span

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
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
