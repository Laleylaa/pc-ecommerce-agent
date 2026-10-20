"""Project actual loop evidence into the existing response and persistence contract."""

from app.agent.boundary import boundary_for_classification
from app.agent.independent_answer import AnswerDraft, AnswerInput
from app.agent.intent import classify_intent
from app.agent.outcomes import normalize_tool_result
from app.agent.query_context import resolve_references
from app.agent.query_loop import QuerySession
from app.agent.result_projection import (
    _catalog_search_projection,
    _order_query_projection,
)
from app.schemas.catalog_search import SearchProduct
from app.schemas.chat import (
    ChatResponse,
    EvidenceItem,
    ExecutionDetails,
    ExecutionGoal,
    ExecutionToolCall,
)
from app.schemas.order import OrderCard
from app.tools.orders import _summary as order_summary


def response_state(
    session: QuerySession,
    facts: AnswerInput,
    draft: AnswerDraft,
    answer_mode: str,
    usage: dict,
) -> tuple[ChatResponse, dict]:
    records = session.adapter.evidence.active_records()
    dispositions = {goal["disposition"] for goal in facts.goals}
    classification = next(
        (
            classification
            for disposition, classification in (
                ("security_refusal", "security_refusal"),
                ("human_handoff", "human_handoff_required"),
                ("out_of_scope", "out_of_scope"),
            )
            if disposition in dispositions
        ),
        "in_scope_auto",
    )
    boundary = boundary_for_classification(classification, "agent_loop_goal_outcomes")
    products, documents, orders, order, parsed = {}, {}, {}, None, {}
    catalog_queries, order_queries = [], []
    catalog_completed = False
    # Multiple selected products do not establish one current product, even if only
    # one lookup succeeds. Never let the last successful call decide that selection.
    selected_products = {
        tuple(sorted(resolved["identity"].items())): resolved["identity"]
        for goal in session.book.goals.values()
        for resolved in resolve_references(session.prepared, goal)
        if resolved["reference"]["kind"] in {"catalog_candidate", "referenced_product"}
    }
    if len(selected_products) > 1:
        parsed["referenced_product"] = {}
    for record in records:
        if not record.result.ok:
            continue
        output = record.result.output or {}
        name = record.invocation.tool_name
        if name == "catalog_search" and output.get("query_state") == "completed":
            if output.get("status") in {"success", "partial", "no_match", "not_found"}:
                catalog_completed = True
                for value in output.get("products", []):
                    product = SearchProduct.model_validate(value)
                    products[product.sku_id] = product
                catalog_queries.append({
                    "operation_id": record.invocation.operation_id,
                    "goal_id": record.invocation.goal_id,
                    "evidence_id": record.evidence_id,
                    "query": _catalog_search_projection(output),
                })
                if len(selected_products) == 1 and output.get("products"):
                    reference = next(iter(selected_products.values()))
                    if any(
                        all(product.get(key) == value for key, value in reference.items())
                        for product in output["products"]
                    ):
                        parsed["referenced_product"] = reference
        elif name in {"policy_search", "knowledge_search"}:
            for value in output.get("documents", []):
                document = EvidenceItem.model_validate(value)
                documents[(document.source_type, document.source_id)] = document
        elif name == "order_lookup" and output.get("result_type") not in {
            "planning_failed",
            "invalid_query",
            "unsupported",
        }:
            if output.get("order"):
                order = OrderCard.model_validate(output["order"])
                orders[order.id] = order_summary(order).model_dump(mode="json")
            for value in output.get("candidates", []):
                orders[value["id"]] = value
            order_queries.append({
                "operation_id": record.invocation.operation_id,
                "goal_id": record.invocation.goal_id,
                "evidence_id": record.evidence_id,
                "query": _order_query_projection(output),
                "context_ids": output.get("focus_order_ids", []),
            })
    if catalog_queries:
        # A union of independently queried candidates has no single shared filter.
        parsed["product_search"] = catalog_queries[0]["query"] if len(catalog_queries) == 1 else {}
    if len(order_queries) == 1:
        parsed["order_query"] = order_queries[0]["query"]
        parsed["order_context_ids"] = order_queries[0]["context_ids"]
    elif order_queries:
        # Do not present the last query's counts/window as describing all results.
        parsed["order_queries"] = order_queries
        order = None
    parsed["order_candidates"] = list(orders.values())
    details = ExecutionDetails(
        goals=[
            ExecutionGoal(
                id=goal["id"],
                query=goal["query"],
                disposition=goal["disposition"],
                task_ids=list(
                    dict.fromkeys(
                        r.invocation.operation_id
                        for r in records
                        if r.invocation.goal_id == goal["id"]
                    )
                ),
            )
            for goal in facts.goals
        ],
        tool_calls=[
            ExecutionToolCall(
                id=r.invocation.invocation_id,
                task_id=r.invocation.operation_id,
                tool_name=r.invocation.tool_name,
                query=str(r.invocation.arguments.get("query") or ""),
                outcome=normalize_tool_result(
                    {
                        "tool_call_id": r.invocation.invocation_id,
                        "name": r.invocation.tool_name,
                        "execution": r.result.model_dump(mode="json"),
                    }
                ).outcome,
                reason=(
                    r.result.error.code
                    if r.result.error
                    else str((r.result.output or {}).get("reason_code", ""))
                ),
                used_in_answer=r.evidence_id in draft.evidence_ids,
                superseded=r.evidence_id not in {item.evidence_id for item in records},
                arguments=r.invocation.arguments,
                result_summary={
                    "evidence_id": r.evidence_id,
                    "completion_blockers": list(r.completion_blockers),
                    "attempt": r.invocation.attempt,
                    "elapsed_seconds": round(r.invocation.elapsed_seconds, 3),
                    **{
                        key: value
                        for key, value in (r.result.output or {}).items()
                        if key
                        in {
                            "status",
                            "result_type",
                            "match_kind",
                            "selection_scope",
                            "total_match_count",
                            "returned_count",
                            "is_exhaustive",
                            "query_state",
                        }
                    },
                },
            )
            for r in session.adapter.evidence.all_records()
        ],
        model_calls=dict(session.budget.calls),
    )
    response = ChatResponse(
        conversation_id=session.prepared.conversation_id,
        answer=draft.response,
        intent=classify_intent(session.prepared.message),
        boundary=boundary,
        products=list(products.values()),
        evidence=list(documents.values()),
        order=order,
        orders=list(orders.values()),
        order_query=parsed.get("order_query"),
        suggested_actions=facts.suggested_actions,
        execution_details=details,
    )
    metadata = response.model_dump(mode="json", exclude={"conversation_id", "memory_changes"})
    state = {
        **metadata,
        "message": session.prepared.message,
        "parsed": parsed,
        "catalog_tool_succeeded": catalog_completed,
        "assistant_metadata": metadata,
        "agent_loop": {
            "goals": facts.goals,
            "stop_reason": session.stop_reason,
            # Model-authored explanations are audit notes, never answer evidence.
            "model_stop_reason": (
                session.book.completion.reason if session.book.completion else None
            ),
            "answer_mode": answer_mode,
            "usage": usage,
            "tool_attempts": session.budget.tool_attempts,
            "rejections": session.rejections,
            "control_rejections": session.control_rejections,
            "query_batches": session.query_batches,
            "query_results": {
                "catalog": catalog_queries,
                "orders": order_queries,
            },
        },
    }
    return response, state
