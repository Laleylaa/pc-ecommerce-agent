"""Project a turn's recorded execution for the authenticated chat history UI."""

from collections.abc import Mapping
from typing import Any

from app.agent.legacy.outcomes import build_subquery_ledger
from app.core.model_usage import model_call_counts
from app.schemas.chat import ExecutionDetails, ExecutionGoal, ExecutionToolCall

_PUBLIC_ARGUMENTS = {"query", "scope", "limit", "category_hint", "order_id", "document_type"}
_SUMMARY_FIELDS = {
    "result_type", "status", "reason_code", "match_kind", "total_match_count",
    "candidate_count", "returned_count", "total_value_count", "matching_complete",
    "is_exhaustive", "search_strategy",
}


def build_execution_details(state: Mapping[str, Any]) -> ExecutionDetails:
    route = state.get("route_plan") or {}
    goals = [
        ExecutionGoal(
            id=goal["id"], query=goal["query"], disposition=goal["disposition"],
            task_ids=[task["id"] for task in goal.get("tasks", [])],
        )
        for goal in route.get("subqueries", [])
    ]
    waves = state.get("tool_waves", [])
    results = {
        result["tool_call_id"]: result.get("execution", {})
        for wave in waves for result in wave.get("results", [])
    }
    # Keep attempts and recovered/superseded calls visible, rather than only final artifacts.
    ledger = state.get("subquery_ledger") or [
        item.model_dump(mode="json") for item in build_subquery_ledger(waves)
    ]
    used = set((state.get("decision") or {}).get("used_tool_call_ids", []))
    calls = []
    for entry in ledger:
        call_id = entry["tool_call_id"]
        execution = results.get(call_id, {})
        value = execution.get("output") or {}
        summary = {key: value[key] for key in sorted(_SUMMARY_FIELDS) if key in value}
        # Do not expose raw errors, internal plans, credentials or the whole tool payload.
        for field in ("products", "documents", "items"):
            if isinstance(value.get(field), list):
                summary[field] = [
                    {key: item[key] for key in ("title", "name", "value", "label") if key in item}
                    for item in value[field][:20] if isinstance(item, dict)
                ]
        calls.append(ExecutionToolCall(
            id=call_id, task_id=entry.get("subquery", ""),
            tool_name=entry["tool_name"], query=entry.get("canonical_query", ""),
            outcome=entry.get("outcome", "unknown"), reason=entry.get("reason", ""),
            used_in_answer=call_id in used,
            superseded=entry.get("status") == "superseded",
            reused=bool(entry.get("reused_from_tool_call_id")),
            arguments={
                key: val for key, val in entry.get("arguments", {}).items()
                if key in _PUBLIC_ARGUMENTS
            },
            result_summary=summary,
        ))
    return ExecutionDetails(goals=goals, tool_calls=calls, model_calls=model_call_counts())
