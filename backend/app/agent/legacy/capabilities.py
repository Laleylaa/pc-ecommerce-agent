from app.agent.legacy.artifacts import ensure_task_runtime, ready_tasks
from app.agent.legacy.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.legacy.routing import RequestRoutePlan, RoutedTask, ready_tool_subqueries

_DIRECT_CAPABILITIES = {
    "catalog_search",
    "catalog_facets",
    "order_lookup",
    "policy_search",
    "knowledge_search",
}


def decision_from_route_capabilities(
    plan: RequestRoutePlan,
    state: dict | None = None,
) -> OrchestratorDecision | None:
    """Compile concrete, structurally valid Router Tasks into the next Tool wave."""
    runtime_state = state or {}
    if state is not None and state.get("route_plan"):
        ensure_task_runtime(runtime_state)
        subqueries = ready_tasks(runtime_state)
    else:
        subqueries = ready_tool_subqueries(plan)
    if not subqueries or not all(_direct_capability_is_safe(item) for item in subqueries):
        return None

    calls = [
        PlannedToolCall(
            id=f"router_{item.id}_{item.capability}",
            name=str(item.capability),
            arguments=_default_arguments(item),
            subquery=item.id,
            canonical_query=item.query,
            tool_query=item.query,
        )
        for item in subqueries
    ]
    return OrchestratorDecision(
        type="tool_calls",
        reason="router_capability_direct_wave",
        tool_calls=calls,
    )


def _direct_capability_is_safe(subquery: RoutedTask) -> bool:
    capability = str(subquery.capability)
    if capability not in _DIRECT_CAPABILITIES:
        return False


    if capability == "catalog_search" and subquery.input_requirements:
        return (
            not subquery.depends_on
            and len(subquery.input_requirements) == 1
            and subquery.input_requirements[0].source
            in {"context_product", "catalog_candidates"}
        )

    if capability == "order_lookup" and subquery.input_requirements:
        requirements = subquery.input_requirements
        if not subquery.depends_on:
            return (
                len(requirements) == 1
                and requirements[0].source in {"context_order", "order_candidates"}
            )
        return bool(requirements) and {
            item.task_id
            for item in requirements
            if item.source == "task_output" and item.task_id is not None
        } == set(subquery.depends_on) and all(
            item.source == "task_output" for item in requirements
        )

    if subquery.depends_on or subquery.input_requirements:
        return False
    return True


def _default_arguments(subquery: RoutedTask) -> dict[str, int | str]:
    capability = str(subquery.capability)
    if capability == "catalog_search":
        return {"limit": 3}
    if capability == "catalog_facets":
        return {"limit": 20}
    if capability == "order_lookup":
        return {}
    if capability == "policy_search":
        return {"limit": 3}
    return {}


__all__ = ["decision_from_route_capabilities"]
