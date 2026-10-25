"""Optional evidence export; all spans remain in memory, never sent to Phoenix."""

import json
import os
from pathlib import Path

import pytest
from app.core import loop_tracing as tracing
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class Evidence:
    def __init__(self):
        self.observations = []
        self.checks = []

    def save(self, label, value):
        self.observations.append({"label": label, "value": value})

    def equal(self, label, actual, expected):
        self.checks.append({"check": label, "comparison": "equal", "actual": actual,
                            "expected": expected, "passed": actual == expected})
        assert actual == expected, label


@pytest.fixture
def evidence(request, monkeypatch):
    case_id = request.node.callspec.id if hasattr(request.node, "callspec") else (
        request.node.name.removeprefix("test_").split("_", 1)[0]
    )
    request.node.user_properties.append(("case_id", case_id))
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False, span_limits=SpanLimits(max_attributes=512))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_provider", provider)
    captured = Evidence()
    try:
        with tracing.step(case_id, root=True):
            yield captured
    finally:
        spans = [json.loads(span.to_json()) for span in exporter.get_finished_spans()]
        provider.shutdown()
        if destination := os.environ.get("ERROR_CONTRACT_EVIDENCE_DIR"):
            path = Path(destination) / f"{case_id}.json"
            path.write_text(json.dumps({
                "case_id": case_id, "nodeid": request.node.nodeid,
                "observations": captured.observations, "checks": captured.checks,
                "spans": spans,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
