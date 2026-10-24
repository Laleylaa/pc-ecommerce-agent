"""Pure domain result / action views shared by both runtime consumers during migration."""

from collections.abc import Mapping
from typing import Any

from app.agent.intent import extract_order_id


def without_pagination_controls(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Project tool facts without exposing storage cursors as conversation capabilities."""
    controls = {"offset", "next_offset", "page", "page_from", "search_page"}
    projected = {k: v for k, v in facts.items() if k not in controls}
    if isinstance(projected.get("query_plan"), Mapping):
        projected["query_plan"] = without_pagination_controls(projected["query_plan"])
    if projected.get("query_mode") in {"page", "remaining"}:
        projected.pop("query_mode")
    if projected.get("selection") == "remaining":
        projected.pop("selection")
    return projected


def _order_query_projection(output: dict[str, Any]) -> dict[str, Any]:
    return without_pagination_controls(
        {
            "result_type": output.get("result_type"),
            "pending_suggestions": [
                {"id": item["order"]["id"], "differences": item["differences"]}
                for item in output.get("suggestions", [])
            ],
            "query_mode": output.get("query_mode") or "recent",
            "total_match_count": int(output.get("total_match_count") or 0),
            "returned_count": int(output.get("returned_count") or 0),
            "is_exhaustive": bool(output.get("is_exhaustive", True)),
        }
    )


def _catalog_search_projection(output: dict[str, Any]) -> dict[str, Any]:
    projection = dict(output.get("query_plan") or {})
    for key in (
        "result_purpose",
        "selection_scope",
        "total_match_count",
        "returned_count",
        "is_exhaustive",
        "ranking_strategy",
        "identity_suggestions",
        "match_kind",
        "matching_complete",
        "requested_count",
        "display_cap",
        "limit_capped",
    ):
        if key in output:
            projection[key] = output[key]
    return without_pagination_controls(projection)


def _handoff_payload(message: str) -> dict[str, Any]:
    return {
        "handoff": True,
        "orderId": extract_order_id(message),
        "requestType": _handoff_request_type(message),
        "reason": message,
    }


def _handoff_request_type(message: str) -> str:
    if "退款" in message:
        return "refund"
    if "退货" in message or "换货" in message:
        return "return"
    if "维修" in message or "保修" in message:
        return "repair"
    if any(term in message for term in ["取消订单", "改地址", "改收货", "修改订单"]):
        return "order_change"
    return "other"
