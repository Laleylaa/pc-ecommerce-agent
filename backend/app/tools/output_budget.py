"""Tool-owned output windows. Never slice JSON, facts, policy clauses or order items."""

from copy import deepcopy

from pydantic import BaseModel

from app.tools.output_limits import MAX_RESULT_ITEMS, OutputBudget, output_cost


class OutputBudgetExceeded(ValueError):
    """Even one complete useful entry plus required facts cannot fit."""


def prepare_tool_output(name: str, output: BaseModel, token_limit: int) -> BaseModel:
    """Apply domain-specific windows, then revalidate the public output contract.

    Called inside the formal tool execution boundary, before returning to any
    runtime. Full output remains in the execution trace; no second query or LLM.
    """
    data = deepcopy(output.model_dump(mode="json"))
    budget = OutputBudget(token_limit=token_limit).model_dump(mode="json")
    data["output_budget"] = budget
    planned_count = data.get("query_plan", {}).get("requested_limit")
    counts = [n for n in (planned_count, data.get("requested_count"))
              if isinstance(n, int) and not isinstance(n, bool) and n > 0]
    budget["requested_item_count"] = max(counts) if counts else None

    def omitted(path: str) -> None:
        budget["truncated"] = True
        if path not in budget["omitted_sections"]:
            budget["omitted_sections"].append(path)
        budget["message"] = (
            "单次输出预算限制：部分明细或辅助信息未展示；全集统计不变。"
            "请说明限制，必要时缩小查询，不自动连续调用凑齐全部结果。"
        )

    def fits() -> bool:
        return output_cost({"tool_name": name, "ok": True, "output": data,
                            "error": None}) <= token_limit

    if budget["requested_item_count"] and budget["requested_item_count"] > MAX_RESULT_ITEMS:
        omitted("requested_display_count")

    # Nested source lists are previews, never the population used for counts.
    if name == "catalog_lookup" and data.get("lookup_summary"):
        for field in data["lookup_summary"]["fields"]:
            for value in field["values"]:
                versions = value["versions"]
                value["total_version_count"] = len(versions)
                value["versions_exhaustive"] = len(versions) <= MAX_RESULT_ITEMS
                if len(versions) > MAX_RESULT_ITEMS:
                    value["versions"] = versions[:MAX_RESULT_ITEMS]
                    omitted("lookup_summary.fields.versions")
    if name == "catalog_facets":
        for item in data["items"]:
            item["total_nested_value_count"] = len(item["values"])
            item["values_exhaustive"] = len(item["values"]) <= MAX_RESULT_ITEMS
            if len(item["values"]) > MAX_RESULT_ITEMS:
                item["values"] = item["values"][:MAX_RESULT_ITEMS]
                omitted("items.values")
            for value in item["values"]:
                for field in ("sku_ids", "spu_ids"):
                    ids = value[field]
                    value[f"total_{field}_count"] = len(ids)
                    if len(ids) > MAX_RESULT_ITEMS:
                        value[field] = ids[:MAX_RESULT_ITEMS]
                        value["sources_exhaustive"] = False
                        omitted(f"items.values.{field}")

    if not fits():
        # Executed plans are audit material; operation/conditions are also used
        # by catalog result invariants and must survive output validation.
        plan = data.get("query_plan")
        if plan and (data.get("query_state") == "completed" or data.get("result_type") in {
            "facets", "single_order", "order_candidates", "order_analysis", "order_suggestions",
        }):
            keep = {"operation", "conditions", "search_text", "name", "scope"}
            compact = {k: v for k, v in plan.items() if k in keep}
            if compact != plan:
                data["query_plan"] = compact
                omitted("query_plan.audit_details")
        # Invalid-query diagnostics contain structured conflict evidence; never
        # remove those. For normal results retain code/message/unknown warnings.
        if data.get("query_state") == "completed" or name == "catalog_facets":
            for diagnostic in data.get("diagnostics", []):
                if diagnostic.get("details"):
                    diagnostic["details"] = {}
                    omitted("diagnostics.details")

    collection = {
        "catalog_search": "products", "catalog_lookup": "products",
        "catalog_facets": "items", "policy_search": "documents",
        "knowledge_search": "documents",
    }.get(name)
    if name == "order_lookup":
        collection = next((key for key in ("analysis_orders", "candidates", "suggestions")
                           if data.get(key)), None)
    entries = data.get(collection, []) if collection else []
    budget["original_item_count"] = len(entries)
    budget["returned_item_count"] = len(entries)

    def update_window() -> None:
        count = len(entries)
        budget["returned_item_count"] = count
        if name in {"catalog_search", "catalog_lookup"}:
            data["returned_count"] = count
            data["is_exhaustive"] = False
            next_offset = (data.get("offset") or 0) + count
            data["next_offset"] = (next_offset if next_offset < data["candidate_count"]
                                   else None)
        elif name == "catalog_facets":
            data["returned_count"] = count
            data["is_exhaustive"] = False
        elif name == "order_lookup":
            if collection == "suggestions":
                data["suggestion_is_exhaustive"] = False
            else:
                data["returned_count"] = count
                data["is_exhaustive"] = False
                data["next_offset"] = (data.get("offset") or 0) + count
                if collection == "analysis_orders":
                    kept = {entry["id"] for entry in entries}
                    data["focus_order_ids"] = [i for i in data["focus_order_ids"] if i in kept]

    cap = 5 if name == "order_lookup" else MAX_RESULT_ITEMS
    while len(entries) > cap or (not fits() and len(entries) > 1):
        entries.pop()
        omitted(collection)
        update_window()

    # Large fact groups are kept intact with their SKU attribution. If the
    # minimal result still exceeds budget, fail explicitly instead of deleting
    # requested facts, logistics events, order items or policy qualifications.
    if not fits():
        raise OutputBudgetExceeded(name)
    validated = type(output).model_validate(data)
    if output_cost({"tool_name": name, "ok": True,
                    "output": validated.model_dump(mode="json"), "error": None}) > token_limit:
        raise OutputBudgetExceeded(name)
    return validated
