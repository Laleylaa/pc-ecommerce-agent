"""Facet aggregation over the same SKU records and field rules as catalog search."""

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from app.repositories.catalog_search import CatalogSearchRepository
from app.rules.product_identity import PRODUCT_REFERENCE_PREFIX, product_reference_kind
from app.schemas.catalog_search import FieldIssue, SearchCondition, SearchPlan, SearchScope
from app.tools.catalog_fields import (
    hard_condition_verdict,
    normalize_category_hint,
    preference_rank,
    same_recorded_value,
)
from app.tools.catalog_search.identity import locate_available, locate_target
from app.tools.catalog_search.matching import check_record
from app.tools.catalog_search.outcomes import ResultReason, completed_policy, policy_for
from app.tools.catalog_search.rules import CatalogNameMatcher
from app.tools.schemas import (
    CatalogFacetInput,
    CatalogFacetItem,
    CatalogFacetOutput,
    CatalogFacetValue,
    ToolDiagnostic,
)

if TYPE_CHECKING:
    from app.tools.catalog import FacetQueryPlan


def explicit_spec_request(request: CatalogFacetInput) -> dict | None:
    """Only a complete object-spec grammar bypasses natural language planning."""
    match = re.fullmatch(
        rf"\s*(?:请)?(?:查一下|查看|查询)?\s*{PRODUCT_REFERENCE_PREFIX}(\d+)\s*"
        r"(?:的)?(?:有什么规格|有哪些规格|规格|参数|有什么参数|有哪些参数)[？?。]?\s*",
        request.query,
        re.IGNORECASE,
    )
    if match:
        return {
            "selection": "object",
            "facet": "spec_key",
            "target": {"kind": product_reference_kind(match[1]), "id": int(match[2])},
        }
    if request.scope and re.fullmatch(
        r"\s*(?:这款|这个商品|这个型号)(?:的)?(?:有什么规格|有哪些规格|规格|参数)"
        r"[？?。]?\s*",
        request.query,
    ):
        return {"selection": "object", "facet": "spec_key"}
    return None


async def execute_facets(
    repository: CatalogSearchRepository,
    request: CatalogFacetInput,
    plan: "FacetQueryPlan",
) -> CatalogFacetOutput:
    base = {
        "facet": plan.facet,
        "category": plan.category,
        "brand": plan.brand,
        "spec_key": plan.spec_key,
        "scope": request.scope,
        "query_plan": plan.model_dump(mode="json"),
    }
    missing_object = plan.selection == "object" and not (plan.target or request.scope)
    if missing_object and (
        plan._query_issue is None or plan._query_issue.reason != ResultReason.INVALID_PLAN
    ):
        return CatalogFacetOutput(
            **base,
            result_type="empty",
            is_exhaustive=False,
            matching_complete=False,
            diagnostics=[
                ToolDiagnostic(
                    code="product_identity_required" if plan.name else "query_needs_clarification",
                    severity="warning",
                    message="Resolve the product through catalog_search before reading its specs",
                    recommended_action=(
                        "resolve_product_with_catalog_search"
                        if plan.name
                        else "ask_user_to_identify_product"
                    ),
                    details={
                        **(plan._query_issue.details if plan._query_issue else {}),
                        "phase": "requirements",
                    },
                )
            ],
        )
    if issue := plan._query_issue:
        diagnostic = policy_for(issue.reason).diagnostic(issue.message)
        diagnostic["details"].update(issue.details)
        return CatalogFacetOutput(
            **base, result_type="empty", is_exhaustive=False, matching_complete=False,
            diagnostics=[ToolDiagnostic(**diagnostic)],
        )
    if plan.clarification_requirements:
        policy = policy_for(ResultReason.REQUIREMENT_NEEDS_CLARIFICATION)
        return CatalogFacetOutput(
            **base,
            result_type="empty",
            is_exhaustive=False,
            matching_complete=False,
            diagnostics=[ToolDiagnostic(**(policy.diagnostic(plan.unsupported_reason) | {
                "details": {
                    "phase": "requirements",
                    "requirements": list(plan.clarification_requirements),
                    "unsupported_requirements": list(plan.unsupported_requirements),
                },
            }))],
        )
    if not plan.supported and not missing_object:
        policy = policy_for(ResultReason(plan.unsupported_code))
        return CatalogFacetOutput(
            **base,
            result_type="empty",
            is_exhaustive=False,
            matching_complete=False,
            diagnostics=[
                ToolDiagnostic(**(policy.diagnostic(plan.unsupported_reason) | {
                    "details": {"phase": "requirements",
                                "requirements": list(plan.unsupported_requirements),
                                "requirement_assessment": plan._requirement_assessment},
                }))
            ],
        )
    scope = request.scope or SearchScope()
    read_scope = (
        SearchScope(**{f"{plan.target.kind}_ids": [plan.target.id]})
        if plan.target
        else (SearchScope() if plan.name else scope)
    )
    # Read IDs independently of filters so a category mismatch cannot make a
    # real object appear nonexistent, inactive or outside the caller's scope.
    object_lookup = plan.target is not None or plan.selection == "object"
    records = await repository.load(
        None if object_lookup else normalize_category_hint(plan.category), read_scope,
    )
    status = "found"
    if plan.target:
        status, records = locate_target(records, scope, plan.name)
    elif plan.name:
        named = CatalogNameMatcher(records).resolve(plan.name, records)
        status, records = locate_available(named, scope)
    elif object_lookup:
        status, records = locate_available(records, scope)
    else:
        records = [r for r in records if r.active]
    if status != "found":
        policy = completed_policy(
            total_matches=0, returned_count=0, unknown_count=0, object_status=status,
        )
        return CatalogFacetOutput(
            **base, result_type="empty", is_exhaustive=status == "not_found",
            matching_complete=status == "not_found",
            diagnostics=[ToolDiagnostic(**(policy.diagnostic() | {
                "details": {
                    "phase": "query_result",
                    "target": plan.target.model_dump() if plan.target else None,
                    "name": plan.name,
                },
            }))],
        )
    if object_lookup and plan.category:
        category = normalize_category_hint(plan.category)
        records = [r for r in records if r.product.category == category]
    if scope.sku_ids:
        records = [r for r in records if r.product.sku_id in scope.sku_ids]
    if scope.spu_ids:
        records = [r for r in records if r.product.spu_id in scope.spu_ids]
    conditions = list(plan.conditions)
    conditions.extend(
        SearchCondition(key=key, values=[value], evidence="trusted override")
        for key, value in plan.filters.items()
    )
    if plan.brand:
        conditions.append(SearchCondition(key="brand", values=[plan.brand], evidence="brand"))
    if plan.min_price is not None:
        conditions.append(
            SearchCondition(
                key="price", operator="gte", values=[str(plan.min_price)], evidence="price"
            )
        )
    if plan.max_price is not None:
        conditions.append(
            SearchCondition(
                key="price", operator="lte", values=[str(plan.max_price)], evidence="price"
            )
        )
    eligible, issues, unknown = [], [], 0
    scores: dict[int, tuple[int, int]] = {}
    condition_plan = SearchPlan(conditions=conditions)
    for record in records:
        checks = check_record(record, condition_plan)
        verdicts = [(c.condition.strength, c.verdict) for c in checks]
        verdict = hard_condition_verdict(verdicts)
        if verdict == "mismatch":
            continue
        issues.extend(record.issues)
        if verdict == "unknown":
            unknown += 1
            continue
        scores[record.product.sku_id] = preference_rank(verdicts)
        eligible.append(record)

    # Every value retains its contributing SKUs; SPU dimensions never imply a combination.
    values: dict[str, dict[str, tuple[set[int], set[int]]]] = defaultdict(dict)
    for record in eligible:
        product = record.product
        conflicted = {i.field for i in record.issues if i.status == "conflict"}
        if plan.facet == "spec_key":
            for key in conflicted:
                if not plan.spec_key or key == plan.spec_key:
                    values.setdefault(key, {})
        if plan.facet in {"category", "brand"}:
            facts = [(None, getattr(product, plan.facet))]
        else:
            facts = [
                (key, value)
                for key, value in product.specs.items()
                if key not in conflicted and (not plan.spec_key or key == plan.spec_key)
            ]
            if plan.spec_key and plan.spec_key not in product.specs:
                issues.append(
                    FieldIssue(sku_id=product.sku_id, field=plan.spec_key, status="missing")
                )
            elif plan.facet == "spec_key" and not product.specs and not conflicted:
                # No recorded dimensions is missing evidence, not proof of no specifications.
                issues.append(FieldIssue(sku_id=product.sku_id, field="specs", status="missing"))
        for key, value in facts:
            dimension = key if plan.facet == "spec_key" else "values"
            bucket = values[dimension]
            canonical = next(
                (v for v in bucket if key and same_recorded_value(key, v, value)), value,
            )
            sku_ids, spu_ids = bucket.setdefault(canonical, (set(), set()))
            sku_ids.add(product.sku_id)
            spu_ids.add(product.spu_id)
    items = []
    for dimension, bucket in values.items():
        if plan.facet == "spec_key":
            skus = set().union(*(ids[0] for ids in bucket.values()))
            spus = set().union(*(ids[1] for ids in bucket.values()))
            items.append(
                CatalogFacetItem(
                    value=dimension,
                    spec_key=dimension,
                    count=len(spus if plan.count_scope == "spu" else skus),
                    count_scope=plan.count_scope,
                    sku_count=len(skus),
                    spu_count=len(spus),
                    values=[
                        CatalogFacetValue(value=v, sku_ids=sorted(ids[0]), spu_ids=sorted(ids[1]))
                        for v, ids in sorted(
                            bucket.items(),
                            key=lambda item: (
                                min(scores[sku] for sku in item[1][0]), item[0]
                            ),
                        )
                    ],
                )
            )
        else:
            items.extend(
                CatalogFacetItem(
                    value=value,
                    spec_key=plan.spec_key,
                    count=len(ids[1] if plan.count_scope == "spu" else ids[0]),
                    count_scope=plan.count_scope,
                    sku_count=len(ids[0]),
                    spu_count=len(ids[1]),
                    values=[
                        CatalogFacetValue(
                            value=value, sku_ids=sorted(ids[0]), spu_ids=sorted(ids[1])
                        )
                    ],
                )
                for value, ids in bucket.items()
            )
    items.sort(key=lambda item: (
        min(
            (scores[sku] for value in item.values for sku in value.sku_ids),
            default=(0, 0),
        ),
        -item.count, item.value,
    ))
    total = len(items)
    items = items[: request.limit]
    diagnostics = []
    if unknown or issues:
        diagnostics.append(
            ToolDiagnostic(
                code="data_insufficient",
                severity="warning",
                message="Missing or conflicting SKU facts limit the result",
                details={"unknown_sku_count": unknown, "phase": "query_result"},
            )
        )
    if not items and not unknown and not issues:
        diagnostics.append(ToolDiagnostic(code="not_found", message="No confirmed facet values",
                                          details={"phase": "query_result"}))
    diagnostics.append(ToolDiagnostic(
        code="requirements_assessed", message="仅说明已提取要求，不证明自然语言提取完整。",
        details={"phase": "query_result", "requirement_assessment": plan._requirement_assessment},
    ))
    return CatalogFacetOutput(
        **base,
        result_type="facets" if items else "empty",
        items=items,
        total_value_count=total,
        returned_count=len(items),
        is_exhaustive=len(items) == total and unknown == 0 and not issues,
        matching_complete=unknown == 0 and not issues,
        field_issues=issues,
        diagnostics=diagnostics,
    )
