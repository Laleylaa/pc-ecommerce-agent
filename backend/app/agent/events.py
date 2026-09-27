"""SSE event and audit-state serialization helpers."""

from typing import Any

from app.agent.state import AgentState
from app.schemas.chat import EvidenceItem


def _dump_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]


def _json_safe_state(state: AgentState) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in state.items():
        if key == "products":
            result[key] = [item.model_dump(mode="json") for item in value]
        elif key == "evidence":
            result[key] = _dump_evidence(value)
        elif key == "order" and value is not None:
            result[key] = value.model_dump(mode="json")
        else:
            result[key] = value
    return result


def _stream_event(event_type: str, state: AgentState, **payload: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"type": event_type}
    if state.get("conversation_id") is not None:
        event["conversation_id"] = state["conversation_id"]
    if state.get("run_id") is not None:
        event["run_id"] = state["run_id"]
    event.update(payload)
    return event


def _context_event(state: AgentState) -> dict[str, Any]:
    return _stream_event(
        "context",
        state,
        intent=state.get("intent"),
        boundary=state.get("boundary"),
        products=[product.model_dump(mode="json") for product in state.get("products", [])],
        order=state["order"].model_dump(mode="json") if state.get("order") else None,
        evidence=_dump_evidence(state.get("evidence", [])),
    )
