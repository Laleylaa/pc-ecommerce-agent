"""Deterministic planner used when the orchestration LLM is unavailable."""

from typing import Any

from app.agent.artifacts import user_clarifiable_blockers
from app.agent.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.intent import classify_intent, extract_order_id
from app.agent.responses import (
    _fallback_catalog_facets_arguments,
)
from app.agent.route_runtime import _resolve_compare_sku_ids, _resolve_order_id
from app.agent.routing import RoutedTask
from app.agent.state import AgentState
from app.agent.tool_loop import (
    _clarification_decision,
    _ready_unattempted_tool_subqueries,
    _state_terminal_decision,
)


def fallback_planner_decision(runtime: Any, state: AgentState) -> OrchestratorDecision:
    """Provide an offline decision without bypassing the routed control flow."""
    if state.get("tool_results"):
        ready_tasks = _ready_unattempted_tool_subqueries(state)
        if ready_tasks:
            return fallback_routed_tool_decision(runtime, state, ready_tasks)
        return _state_terminal_decision(state, "llm_not_configured")

    routed_subqueries = _ready_unattempted_tool_subqueries(state)
    if routed_subqueries:
        return fallback_routed_tool_decision(runtime, state, routed_subqueries)
    blockers = user_clarifiable_blockers(state)
    if blockers:
        missing = blockers[0].get("missing_information") or ["具体商品或订单信息"]
        return _clarification_decision(
            f"请补充{str(missing[0])}，我再继续查询。",
            "runtime_missing_user_suppliable_artifact",
        )
    return _clarification_decision(
        "我还不能准确判断需要查询的业务信息，请补充具体商品、订单或政策问题。",
        "routed_fallback_without_tool_subquery",
    )

def fallback_routed_tool_decision(
    runtime: Any,
    state: AgentState,
    subqueries: list[RoutedTask],
) -> OrchestratorDecision:
    calls: list[PlannedToolCall] = []
    for subquery in subqueries:
        query = subquery.query
        capability = str(subquery.capability)
        if capability == "catalog_search":
            name = capability
            arguments = {"query": query, "limit": 3}
        elif capability == "catalog_compare":
            name = capability
            arguments = {"query": query, "limit": 5}
        elif capability == "catalog_facets":
            name = capability
            arguments = _fallback_catalog_facets_arguments(query) or {
                "query": query,
                "limit": 20,
            }
        elif capability == "order_lookup":
            name = capability
            arguments = {
                "query": query,
                "order_id": None,
            }
        elif capability in {"policy_search", "knowledge_search"}:
            name = capability
            arguments = {"query": query, "limit": 3}
        else:
            # Only an explicitly ambiguous Router Task may fall back to text inference.
            facet_arguments = _fallback_catalog_facets_arguments(query)
            if facet_arguments:
                name = "catalog_facets"
                arguments = facet_arguments
            else:
                intent = classify_intent(query)
                if intent == "product_recommendation":
                    compare_sku_ids = _resolve_compare_sku_ids(
                        state["message"], state.get("working_memory_snapshot", {})
                    )
                    is_compare_request = any(
                        marker in state["message"].casefold()
                        for marker in ("对比", "比较", "区别", "哪个好", "vs")
                    )
                    if is_compare_request and compare_sku_ids:
                        name = "catalog_compare"
                        arguments = {"query": query, "limit": 5}
                    else:
                        name = "catalog_search"
                        arguments = {"query": query, "limit": 3}
                elif intent == "order_status":
                    name = "order_lookup"
                    arguments = {
                        "query": query,
                        "order_id": _resolve_order_id(
                            query,
                            extract_order_id(query),
                            state.get("working_memory_snapshot", {}),
                            runtime.memory_service,
                        ),
                    }
                elif intent == "after_sales":
                    name = "policy_search"
                    arguments = {
                        "query": query,
                        "limit": 3,
                    }
                else:
                    name = "knowledge_search"
                    arguments = {
                        "query": query,
                        "limit": 3,
                    }
        calls.append(
            PlannedToolCall(
                id=f"fallback_{subquery.id}_{name}",
                name=name,
                arguments=arguments,
                subquery=subquery.id,
            )
        )
    return OrchestratorDecision(type="tool_calls", tool_calls=calls)
