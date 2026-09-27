from typing import Any

from app.agent.decisions import PlannedToolCall
from app.agent.outcomes import is_active_ledger_entry
from app.agent.state import AgentState
from app.schemas.catalog import ProductCard
from app.schemas.chat import EvidenceItem
from app.schemas.order import OrderCard
from app.tools.schemas import ToolExecutionResult


def apply_tool_output(
    state: AgentState,
    call: PlannedToolCall,
    execution: ToolExecutionResult,
) -> None:
    if call.name in {"catalog_search", "catalog_compare"}:
        state["catalog_tool_succeeded"] = _catalog_execution_completed(
            execution.model_dump(mode="python")
        )
    if not execution.ok or not execution.output:
        return
    output = execution.output
    if call.name in {"catalog_search", "catalog_compare"}:
        state["products"] = [
            ProductCard.model_validate(product) for product in output.get("products", [])
        ]
        if call.name == "catalog_search":
            state.setdefault("parsed", {})["product_search"] = _catalog_search_projection(
                output
            )
        else:
            state.setdefault("parsed", {})["catalog_comparison"] = {
                "query": call.arguments.get("query"),
                "comparison_level": output.get("comparison_level") or "sku",
                "sku_ids": [product.sku_id for product in state["products"]],
                "spu_ids": [
                    item.get("spu_id")
                    for item in output.get("series", [])
                    if isinstance(item, dict)
                ],
                "comparison_fields": output.get("comparison_fields", []),
            }
    elif call.name in {"policy_search", "knowledge_search"}:
        evidence = [
            EvidenceItem.model_validate(document) for document in output.get("documents", [])
        ]
        state["evidence"] = dedupe_evidence([*state.get("evidence", []), *evidence])
    elif call.name == "order_lookup":
        if output.get("order"):
            state["order"] = OrderCard.model_validate(output["order"])
        parsed = state.setdefault("parsed", {})
        parsed["order_candidates"] = output.get("candidates", [])
        parsed["order_context_ids"] = output.get("focus_order_ids", [])
        parsed["order_query"] = _order_query_projection(output)


def rebuild_tool_projections(state: AgentState) -> None:
    """Rebuild compatibility state from active observations instead of the latest call."""
    if "task_artifacts" in state:
        _rebuild_from_task_artifacts(state)
        return
    active_ids = {
        str(entry.get("tool_call_id"))
        for entry in state.get("subquery_ledger", [])
        if is_active_ledger_entry(entry) and entry.get("tool_call_id")
    }
    if not active_ids:
        return

    products: list[ProductCard] = []
    seen_sku_ids: set[int] = set()
    evidence: list[EvidenceItem] = []
    order: OrderCard | None = None
    order_candidates: list[dict[str, Any]] = []
    order_query: dict[str, Any] | None = None
    parsed = state.setdefault("parsed", {})
    parsed.pop("product_search", None)
    parsed.pop("catalog_comparison", None)
    parsed.pop("order_candidates", None)
    parsed.pop("order_context_ids", None)
    parsed.pop("order_query", None)
    saw_catalog_result = False
    catalog_completed = False

    for result in state.get("tool_results", []):
        call_id = str(result.get("tool_call_id") or "")
        if call_id not in active_ids:
            continue
        name = str(result.get("name") or "")
        execution = result.get("execution", {})
        if not isinstance(execution, dict):
            continue
        output = execution.get("output")
        if name in {"catalog_search", "catalog_compare"}:
            saw_catalog_result = True
            catalog_completed = catalog_completed or _catalog_execution_completed(
                execution
            )
        if not execution.get("ok") or not isinstance(output, dict):
            continue

        if name in {"catalog_search", "catalog_compare"}:
            for item in output.get("products", []):
                product = ProductCard.model_validate(item)
                if product.sku_id in seen_sku_ids:
                    continue
                seen_sku_ids.add(product.sku_id)
                products.append(product)
            if name == "catalog_search":
                parsed["product_search"] = _catalog_search_projection(output)
            else:
                parsed["catalog_comparison"] = {
                    "query": tool_call_arguments(state, call_id).get("query"),
                    "comparison_level": output.get("comparison_level") or "sku",
                    "sku_ids": [item.get("sku_id") for item in output.get("products", [])],
                    "spu_ids": [
                        item.get("spu_id")
                        for item in output.get("series", [])
                        if isinstance(item, dict)
                    ],
                    "comparison_fields": output.get("comparison_fields", []),
                }
        elif name in {"policy_search", "knowledge_search"}:
            evidence.extend(
                EvidenceItem.model_validate(item)
                for item in output.get("documents", [])
            )
        elif name == "order_lookup":
            if output.get("order"):
                order = OrderCard.model_validate(output["order"])
            candidates = output.get("candidates")
            if isinstance(candidates, list):
                order_candidates = candidates
            parsed["order_context_ids"] = output.get("focus_order_ids", [])
            order_query = _order_query_projection(output)

    state["products"] = products
    state["evidence"] = dedupe_evidence(evidence)
    state["order"] = order
    parsed["order_candidates"] = order_candidates
    if order_query is not None:
        parsed["order_query"] = order_query
    if saw_catalog_result:
        state["catalog_tool_succeeded"] = catalog_completed


def _rebuild_from_task_artifacts(state: AgentState) -> None:
    """Project only usable run-local artifacts into the legacy API/memory fields."""
    products: list[ProductCard] = []
    seen_sku_ids: set[int] = set()
    evidence: list[EvidenceItem] = []
    order: OrderCard | None = None
    order_candidates: list[dict[str, Any]] = []
    order_query: dict[str, Any] | None = None
    parsed = state.setdefault("parsed", {})
    parsed.pop("product_search", None)
    parsed.pop("catalog_comparison", None)
    parsed.pop("order_candidates", None)
    parsed.pop("order_context_ids", None)
    parsed.pop("order_query", None)
    saw_catalog = False

    for artifact in state.get("task_artifacts", {}).values():
        if not isinstance(artifact, dict) or not artifact.get("usable"):
            continue
        value = artifact.get("value")
        if not isinstance(value, dict):
            continue
        tool_name = str(artifact.get("source_tool_name") or "")
        call_id = str(artifact.get("source_tool_call_id") or "")
        if tool_name in {"catalog_search", "catalog_compare"}:
            saw_catalog = True
            for item in value.get("products", []):
                product = ProductCard.model_validate(item)
                if product.sku_id in seen_sku_ids:
                    continue
                seen_sku_ids.add(product.sku_id)
                products.append(product)
            if tool_name == "catalog_search":
                parsed["product_search"] = _catalog_search_projection(value)
            else:
                parsed["catalog_comparison"] = {
                    "query": tool_call_arguments(state, call_id).get("query"),
                    "comparison_level": value.get("comparison_level") or "sku",
                    "sku_ids": value.get("selected_sku_ids") or [],
                    "spu_ids": value.get("selected_spu_ids") or [],
                    "comparison_fields": value.get("comparison_fields") or [],
                }
        elif tool_name in {"policy_search", "knowledge_search"}:
            evidence.extend(
                EvidenceItem.model_validate(item)
                for item in value.get("documents", [])
            )
        elif tool_name == "order_lookup":
            if value.get("order"):
                order = OrderCard.model_validate(value["order"])
            candidates = value.get("candidates")
            if isinstance(candidates, list):
                order_candidates = candidates
            parsed["order_context_ids"] = value.get("focus_order_ids", [])
            order_query = _order_query_projection(value)

    state["products"] = products
    state["evidence"] = dedupe_evidence(evidence)
    state["order"] = order
    parsed["order_candidates"] = order_candidates
    if order_query is not None:
        parsed["order_query"] = order_query
    if saw_catalog:
        state["catalog_tool_succeeded"] = True


def _catalog_execution_completed(execution: dict[str, Any]) -> bool:
    if not execution.get("ok"):
        return False
    output = execution.get("output")
    if not isinstance(output, dict):
        return False
    diagnostics = output.get("diagnostics")
    if not isinstance(diagnostics, list):
        return True
    return not any(
        isinstance(item, dict) and item.get("severity") == "error"
        for item in diagnostics
    )


def _order_query_projection(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "result_type": output.get("result_type"),
        "query_mode": output.get("query_mode") or "recent",
        "total_match_count": int(output.get("total_match_count") or 0),
        "returned_count": int(output.get("returned_count") or 0),
        "is_exhaustive": bool(output.get("is_exhaustive", True)),
        "offset": int(output.get("offset") or 0),
        "next_offset": output.get("next_offset"),
    }


def _catalog_search_projection(output: dict[str, Any]) -> dict[str, Any]:
    projection = dict(output.get("query_plan") or {})
    for key in (
        "result_purpose",
        "selection_scope",
        "total_match_count",
        "returned_count",
        "is_exhaustive",
        "ranking_strategy",
    ):
        if key in output:
            projection[key] = output[key]
    return projection


def tool_call_arguments(state: AgentState, call_id: str) -> dict[str, Any]:
    for wave in state.get("tool_waves", []):
        for call in wave.get("calls", []):
            if str(call.get("id") or "") == call_id:
                arguments = call.get("arguments")
                return arguments if isinstance(arguments, dict) else {}
    return {}


def dedupe_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    return list({(item.source_type, item.source_id): item for item in items}.values())


__all__ = ["apply_tool_output", "dedupe_evidence", "rebuild_tool_projections"]
