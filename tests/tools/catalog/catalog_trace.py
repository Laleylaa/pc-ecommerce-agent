"""Read-only Phoenix transport and raw evidence extraction."""

import json
import urllib.request


def get_json(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=10) as response:
        return json.load(response)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def status(rows, empty="无法判定"):
    values = [r["status"] for r in rows]
    if not values:
        return empty
    if "失败" in values:
        return "失败"
    return "通过" if all(v == "通过" for v in values) else "无法判定"


def decode(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


def extract(spans):
    evidence = {"models": [], "validations": [], "events": [], "tools": [], "roots": []}
    for span in sorted(spans, key=lambda s: s["start_time"]):
        attrs = span.get("attributes", {})
        row = {
            "span_id": span["context"]["span_id"],
            "parent_id": span.get("parent_id"),
            "trace_id": span["context"]["trace_id"],
            "name": span["name"],
            "input": decode(attrs.get("input.value")),
            "output": decode(attrs.get("output.value")),
        }
        if not span.get("parent_id"):
            evidence["roots"].append(
                row
                | {
                    "batch_id": attrs.get("batch_id"),
                    "case_id": attrs.get("case_id"),
                    "attempt": attrs.get("attempt"),
                }
            )
        if span.get("span_kind") == "LLM":
            evidence["models"].append(
                row
                | {
                    "stage": attrs.get("stage"),
                    "attempt": attrs.get("attempt"),
                    "messages": {
                        k: v
                        for k, v in attrs.items()
                        if k.startswith(("llm.input_messages.", "llm.output_messages."))
                    },
                    "input_truncated": attrs.get("trace.input.truncated"),
                    "output_truncated": attrs.get("trace.output.truncated"),
                }
            )
        if span.get("span_kind") == "TOOL":
            evidence["tools"].append(row)
        if attrs.get("record_type") == "program_validation":
            evidence["validations"].append(
                row
                | {
                    "validation": attrs.get("validation_name"),
                    "outcome": attrs.get("validation_outcome"),
                    "schema": attrs.get("schema"),
                    "model_span_id": attrs.get("model_span_id"),
                }
            )
        for event in span.get("events", []):
            event_attrs = event.get("attributes", {})
            evidence["events"].append(
                {
                    "span_id": row["span_id"],
                    "name": event["name"],
                    "attributes": {k: decode(v) for k, v in event_attrs.items()},
                }
            )
    return evidence
