"""Handwritten business populations for output-budget acceptance; no IO or LLM."""

from datetime import UTC, datetime

from app.schemas.catalog_search import CatalogSearchResult
from app.tools.schemas import (
    CatalogFacetOutput,
    DocumentSearchOutput,
    OrderLookupOutput,
    WebSearchOutput,
)


def catalog_output(operation="search", *, count=10, padding=0, diagnostic=0):
    products = [
        {
            "sku_id": i,
            "spu_id": 1,
            "title": f"版本{i}",
            "brand": "测试",
            "category": "键盘",
            "price": str(100 + i),
            "stock": i,
            "entity_scope": "sku",
            "candidate_sku_ids": [i],
            "model_sku_count": count,
            "match_kind": "exact",
            "description": "规格说明" * padding,
        }
        for i in range(1, count + 1)
    ]
    summary = None
    if operation == "lookup":
        representatives = [
            products[i - 1] for i in sorted({1, (count + 1) // 2, count})
        ]
        # Summary examples use a separate, minimal version contract.
        summary = {
            "exists": True,
            "matching_complete": True,
            "matched_sku_count": count,
            "unknown_sku_count": 0,
            "min_price": "101",
            "max_price": str(100 + count),
            "price_level_count": count,
            "representative_versions": [
                {k: p[k] for k in ("sku_id", "title", "price", "stock")}
                for p in representatives
            ],
            "fields": [],
        }
    return CatalogSearchResult.model_validate(
        {
            "result_type": "products",
            "status": "success",
            "reason_code": "matches_found",
            "recommended_action": "use_result",
            "query_state": "completed",
            "result_purpose": operation,
            "selection_scope": "sku",
            "products": products[:10],
            "match_kind": "exact",
            "identity_match_count": 1,
            "candidate_count": count,
            "candidate_spu_count": 1,
            "candidate_sku_count": count,
            "total_match_count": count,
            "matched_spu_count": 1,
            "matched_sku_count": count,
            "unknown_sku_count": 0,
            "conflicted_sku_count": 0,
            "missing_fact_sku_count": 0,
            "matching_complete": True,
            "is_exhaustive": count <= 10,
            "data_quality": "clear",
            "offset": 0,
            "limit": min(count, 10),
            "requested_count": min(count, 10),
            "returned_count": min(count, 10),
            "next_offset": 10 if count > 10 else None,
            "query_plan": {
                "operation": operation,
                "conditions": [],
                "search_text": "测试型号",
            },
            "lookup_summary": summary,
            "diagnostics": [
                {
                    "code": "audit",
                    "message": "执行记录",
                    "details": {"audit": "x" * diagnostic},
                }
            ],
        }
    )


def facets_output(*, nested=12, sources=30):
    return CatalogFacetOutput.model_validate(
        {
            "result_type": "facets",
            "facet": "spec_key",
            "total_value_count": 1,
            "returned_count": 1,
            "items": [
                {
                    "value": "color",
                    "spec_key": "color",
                    "count": 3,
                    "sku_count": sources,
                    "spu_count": 3,
                    "values": [
                        {
                            "value": f"颜色{i}",
                            "sku_ids": list(range(1, sources + 1)),
                            "spu_ids": [1, 2, 3],
                        }
                        for i in range(nested)
                    ],
                }
            ],
        }
    )


def document_output(name="policy_search", *, count=3, length=300):
    cls = DocumentSearchOutput if name == "policy_search" else WebSearchOutput
    return cls.model_validate(
        {
            "result_type": "documents",
            "documents": [
                dict(
                    source_id=i,
                    title=f"依据{i}",
                    document_type="policy"
                    if name == "policy_search"
                    else "peripheral_knowledge",
                    snippet="适用前提：包装完好。" + "证据" * length + "例外：定制品。",
                    score=1 / i,
                    **(
                        {"url": f"https://example.com/{i}"}
                        if name != "policy_search"
                        else {}
                    ),
                )
                for i in range(1, count + 1)
            ],
        }
    )


def order_output(*, count=5, padding=250, mode="analysis"):
    orders = [
        {
            "id": i,
            "status": 1,
            "status_label": "待发货",
            "pay_amount": "100",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "items": [
                {
                    "id": i,
                    "sku_id": i,
                    "sku_name": "商品" + "x" * padding,
                    "sku_specs": {},
                    "price": "100",
                    "quantity": 1,
                }
            ],
            "logistics": {
                "express_company": "示例",
                "logistic_no": "example",
                "status": 0,
                "trace": [{"event": "待揽收"}],
            },
        }
        for i in range(1, count + 1)
    ]
    if mode == "single":
        return OrderLookupOutput(
            result_type="single_order",
            order=orders[0],
            total_match_count=1,
            returned_count=1,
        )
    if mode == "suggestions":
        return OrderLookupOutput(
            result_type="order_suggestions",
            suggestions=[
                {
                    "order": o,
                    "differences": [
                        {
                            "field": "statuses",
                            "expected": {"status": 2},
                            "actual": 1,
                            "state": "not_matched",
                        }
                    ],
                }
                for o in orders
            ],
            suggestion_total_count=count,
        )
    return OrderLookupOutput(
        result_type="order_analysis",
        analysis_orders=orders,
        focus_order_ids=list(range(1, count + 1)),
        total_match_count=count,
        returned_count=count,
        is_exhaustive=True,
    )
