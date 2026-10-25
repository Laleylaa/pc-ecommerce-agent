"""Explicit wire fixtures for valid model responses; no production schema defaults."""

from copy import deepcopy


def search_plan(data: dict | None = None) -> dict:
    result = {
        "operation": "search",
        "disposition": "ready",
        "category": None,
        "search_text": None,
        "target": None,
        "version_view": "summary",
        "candidate_unit": "spu",
        "conditions": [],
        "requested_fields": [],
        "unresolved_requirements": [],
        "unsupported_requirements": [],
        "clarification_requirements": [],
        "sort": "relevance",
        "purpose": "search",
    } | deepcopy(data or {})
    if result["operation"] == "lookup" and "purpose" not in (data or {}):
        result["purpose"] = "lookup"
    result["conditions"] = [
        {"operator": "eq", "strength": "requirement", "preference_evidence": None} | c
        for c in result["conditions"]
    ]
    return result


def facet_plan(data: dict | None = None) -> dict:
    result = {
        "selection": "catalog",
        "facet": "brand",
        "category": None,
        "brand": None,
        "name": None,
        "target": None,
        "spec_key": None,
        "filters": {},
        "conditions": [],
        "min_price": None,
        "max_price": None,
        "count_scope": "spu",
        "supported": True,
        "unsupported_reason": None,
        "unresolved_requirements": [],
        "unsupported_requirements": [],
        "clarification_requirements": [],
    } | deepcopy(data or {})
    result["conditions"] = search_plan({"conditions": result["conditions"]})["conditions"]
    return result


def order_plan(data: dict | None = None) -> dict:
    return {
        "selection": "all",
        "operation": "list",
        "requested_fields": ["order_summary"],
        "filters": {
            "time_field": "created_at",
            "start_at": None,
            "end_at": None,
            "statuses": [],
            "product_terms": [],
        },
        "filter_evidence": [],
        "unresolved_requirements": [],
    } | deepcopy(data or {})
