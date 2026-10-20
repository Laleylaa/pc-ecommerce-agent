"""Resolve trusted identities, without turning historical prices into current facts."""

from app.agent.query_schema import CandidateReference, ContextReference, QueryGoal
from app.agent.query_state import GoalBook
from app.agent.tool_adapter import AdmissionError
from app.rules.product_identity import explicit_product_ids
from app.schemas.context import PreparedTurn
from app.tools.order_identity import explicit_order_ids
from app.tools.schemas import CatalogDependencyArtifact, CatalogToolRuntimeContext


def resolve_reference(prepared: PreparedTurn, reference: ContextReference) -> dict:
    """Look up a model-supplied structured selector, without interpreting user prose."""
    memory = prepared.working_memory
    kind = reference.kind
    if isinstance(reference, CandidateReference):
        items = (
            memory.catalog.candidate_display
            if kind == "catalog_candidate"
            else memory.order.candidate_order_ids
        )
        if kind == "catalog_candidate" and not items:
            # Older snapshots stored ordered SKU identities without display cards.
            ids = memory.catalog.candidate_sku_ids
            if reference.position <= len(ids):
                return {"sku_id": ids[reference.position - 1]}
        if reference.position > len(items):
            raise AdmissionError("reference_ambiguous_or_missing")
        item = items[reference.position - 1]
        if kind == "order_candidate":
            return {"order_id": item}
        return {"spu_id": item.spu_id} if item.entity_scope == "spu" else {"sku_id": item.sku_id}
    if kind == "last_order" and memory.order.last_order_id:
        return {"order_id": memory.order.last_order_id}
    if kind == "referenced_product":
        if memory.catalog.referenced_sku_id:
            return {"sku_id": memory.catalog.referenced_sku_id}
        if memory.catalog.referenced_spu_id:
            return {"spu_id": memory.catalog.referenced_spu_id}
    raise AdmissionError("reference_ambiguous_or_missing")


def resolve_references(prepared: PreparedTurn, goal: QueryGoal) -> list[dict]:
    return [
        {"reference": reference.model_dump(mode="json"),
         "identity": resolve_reference(prepared, reference)}
        for reference in goal.references
    ]


def validate_identities(
    prepared: PreparedTurn,
    book: GoalBook,
    goal: QueryGoal,
    name: str,
    arguments: dict,
) -> None:
    if name == "knowledge_search":
        query = arguments.get("query", "")
        # The external web provider never needs private order identities or tracking numbers.
        private_values = set()
        for record in book.evidence.active_records():
            order = (record.result.output or {}).get("order") or {}
            tracking = (order.get("logistics") or {}).get("logistic_no")
            if tracking:
                private_values.add(str(tracking))
        if explicit_order_ids(query) or any(value in query for value in private_values):
            raise AdmissionError("private_order_data_disallowed_in_web_query")
    products = explicit_product_ids(prepared.message)
    orders = explicit_order_ids(prepared.message)
    for resolved in resolve_references(prepared, goal):
        reference = resolved["identity"]
        for kind in ("sku", "spu"):
            if reference.get(f"{kind}_id"):
                products.add((kind, reference[f"{kind}_id"]))
        if reference.get("order_id"):
            orders.add(reference["order_id"])
    source_goals = {goal.id, *(p.goal_id for p in goal.prerequisites)}
    for record in book.evidence.active_records():
        if record.invocation.goal_id not in source_goals or not record.result.ok:
            continue
        output = record.result.output or {}
        for product in output.get("products", []):
            products.add(("spu", product["spu_id"]))
            products.add(("sku", product["sku_id"]))
            products.update(("sku", sku) for sku in product.get("candidate_sku_ids", []))
        if output.get("order"):
            orders.add(output["order"]["id"])
        orders.update(item["id"] for item in output.get("candidates", []))
        orders.update(item["id"] for item in output.get("analysis_orders", []))
        # Relaxed order suggestions are deliberately not trusted automatic inputs.
    if name in {"catalog_search", "catalog_facets"}:
        requested = explicit_product_ids(arguments.get("query", ""))
        scope = arguments.get("scope") or {}
        for kind in ("sku", "spu"):
            requested.update((kind, value) for value in scope.get(f"{kind}_ids", []))
        if not requested <= products:
            raise AdmissionError("unsourced_product_identity")
    if name == "order_lookup":
        requested = explicit_order_ids(arguments.get("query") or "")
        if arguments.get("order_id") is not None:
            requested.add(arguments["order_id"])
        if not requested <= orders:
            raise AdmissionError("unsourced_order_identity")


def catalog_context(prepared: PreparedTurn, book: GoalBook, goal: QueryGoal):
    artifacts = []
    for prerequisite in goal.prerequisites:
        for record in book.records(prerequisite.goal_id):
            output = record.result.output or {}
            if record.result.ok and output.get("products"):
                artifacts.append(
                    CatalogDependencyArtifact(
                        task_id=record.invocation.operation_id,
                        artifact_type="products",
                        products=output["products"],
                        selection_scope=output.get("selection_scope"),
                        selected_sku_ids=[p["sku_id"] for p in output["products"]],
                        selected_spu_ids=list(
                            dict.fromkeys(p["spu_id"] for p in output["products"])
                        ),
                    )
                )
    return CatalogToolRuntimeContext(
        memory=prepared.working_memory.catalog.model_copy(deep=True),
        dependency_artifacts=artifacts[:8],
    )


def query_context(prepared: PreparedTurn) -> dict:
    return {
        "message": prepared.message,
        "history": [message.model_dump(mode="json") for message in prepared.history],
        "preferences": [memory.model_dump(mode="json") for memory in prepared.memory],
        "working_memory": prepared.working_memory.model_dump(mode="json"),
        "context_rule": "历史仅作指代/改写，当前要求优先；价格、库存、物流重新查询。",
    }
