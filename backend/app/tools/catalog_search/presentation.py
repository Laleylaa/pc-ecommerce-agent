"""Map executed search facts to the public result; no candidate selection or I/O."""

from app.core.loop_tracing import program_event
from app.repositories.catalog_search import CatalogRecord
from app.schemas.catalog import ProductVariantCard
from app.schemas.catalog_search import (
    CatalogLookupSummary,
    CatalogSearchRequest,
    CatalogSearchResult,
    CatalogVersionIdentity,
    FieldIssue,
    LookupFieldFacts,
    LookupFieldValue,
    SearchDiagnostic,
    SearchPlan,
    SearchProduct,
)
from app.tools.catalog_fields import FACT_FIELDS, hard_condition_verdict, same_recorded_value
from app.tools.catalog_search.engine import SearchExecution
from app.tools.catalog_search.limits import MAX_VARIANT_PREVIEW
from app.tools.catalog_search.matching import field_value
from app.tools.catalog_search.outcomes import completed_policy, data_quality, fact_diagnostics
from app.tools.catalog_search.requirements import requirement_assessment


def build_result(
    request: CatalogSearchRequest, plan: SearchPlan,
    execution: SearchExecution, planner_name: str,
) -> CatalogSearchResult:
    records, object_results = execution.selection.records, execution.selection.object_results
    unknown, match_kind = execution.selection.unknown, execution.selection.match_kind
    checks = execution.selection.checks
    fallback_attempted = execution.selection.fallback_attempted
    identity_match_count = execution.selection.identity_match_count
    page, model_records = execution.page, execution.model_records
    sku_scope, has_preferences = execution.sku_scope, execution.has_preferences
    preference_scores = execution.preference_scores
    total, limit = execution.total, execution.limit
    window = {
        "selection_scope": "sku" if sku_scope else "spu",
        "offset": execution.offset,
        "limit": limit,
        "returned_count": len(page),
        "next_offset": execution.next_offset,
        "requested_count": execution.requested_count,
        "limit_capped": execution.requested_count is not None and execution.requested_count > limit,
        "query_plan": plan.model_dump(mode="json")
        | {
            "scope": request.scope.model_dump()
            if request.scope.sku_ids or request.scope.spu_ids
            else None
        },
        "planner": planner_name,
        "result_purpose": "lookup" if plan.operation != "search" else plan.purpose,
    }
    fact_fields = list(
        dict.fromkeys([*plan.requested_fields, *(c.key for c in plan.conditions)])
    )
    cards = []
    for group in page:
        model = [
            r for r in model_records if r.active and r.product.spu_id == group[0].product.spu_id
        ]
        card = make_card(
            group, model, bool(sku_scope), fact_fields,
            preferred_ranks=preference_scores if has_preferences else None,
            representative_preview=plan.operation == "lookup" and plan.version_view == "summary",
        )
        card.match_kind = match_kind
        card.condition_checks = [
            c for r in group for c in (checks or {}).get(r.product.sku_id, [])
            if c.sku_id in card.candidate_sku_ids
        ]
        cards.append(card)
    # Assess the entire selected pool, including requested object facts.
    all_issues = [record_issues(r, fact_fields) for r in records]
    conflicted = sum(any(i.status == "conflict" for i in issues) for issues in all_issues)
    missing = sum(any(i.status == "missing" for i in issues) for issues in all_issues)
    policy = completed_policy(
        total_matches=total if match_kind == "exact" else 0,
        returned_count=len(cards) if match_kind == "exact" else 0,
        alternative_count=total if match_kind == "alternative" else 0,
        object_fact_count=total if match_kind == "object_facts" else 0,
        unknown_count=unknown,
        identity_pending=bool(execution.selection.suggestions),
        object_status=object_results[0].status if object_results else None,
    )
    diagnostic = policy.diagnostic()
    diagnostic["details"] = {
        "phase": "query_result",
        "requirement_assessment": requirement_assessment(plan, []),
    }
    diagnostics = [SearchDiagnostic(**diagnostic)]
    diagnostics.extend(
        SearchDiagnostic(**item, details={"phase": "query_result"})
        for item in fact_diagnostics(conflicted, missing, unknown, action=policy.action)
    )
    if execution.requested_count is not None and execution.requested_count > limit:
        diagnostics.append(SearchDiagnostic(
            code="display_limit_capped",
            message=f"请求展示 {execution.requested_count} 项，系统单次最多展示 {limit} 项。",
            details={"requested_count": execution.requested_count, "effective_limit": limit},
        ))
    result = CatalogSearchResult(
        **window,
        lookup_summary=lookup_summary(plan, execution),
        result_type="products" if cards else "empty",
        **policy.fields(),
        query_state="completed",
        products=cards,
        match_kind=match_kind,
        fallback_attempted=fallback_attempted,
        identity_match_count=identity_match_count,
        candidate_count=total,
        candidate_spu_count=len({r.product.spu_id for r in records}),
        candidate_sku_count=len(records),
        object_results=object_results,
        identity_suggestions=execution.selection.suggestions,
        total_match_count=total if match_kind == "exact" else 0,
        matched_spu_count=len({r.product.spu_id for r in records})
        if match_kind == "exact"
        else 0,
        matched_sku_count=len(records) if match_kind == "exact" else 0,
        unknown_sku_count=unknown,
        matching_complete=unknown == 0 and not execution.selection.suggestions,
        is_exhaustive=(
            match_kind in {"exact", "none"}
            and unknown == 0
            and not execution.selection.suggestions
            and execution.offset == 0
            and len(cards) == total
        ),
        data_quality=data_quality(conflicted, missing),
        conflicted_sku_count=conflicted,
        missing_fact_sku_count=missing,
        ranking_strategy=execution.ranking_strategy,
        diagnostics=diagnostics,
    )
    trace_result(result)
    return result


def representative_records(records: list[CatalogRecord]) -> list[CatalogRecord]:
    ordered = sorted(records, key=lambda r: (r.product.price, r.product.sku_id))
    if not ordered:
        return []
    # Lower middle for even populations; stable SKU ID resolves equal prices.
    positions = sorted({0, (len(ordered) - 1) // 2, len(ordered) - 1})
    return [ordered[i] for i in positions]


def variant_card(record: CatalogRecord) -> ProductVariantCard:
    p = record.product
    return ProductVariantCard(
        sku_id=p.sku_id, title=p.title, price=p.price, stock=p.stock,
        specs=p.specs, sku_sales_count=p.sku_sales_count, image_url=p.image_url,
    )


def lookup_summary(plan: SearchPlan, execution: SearchExecution) -> CatalogLookupSummary | None:
    selection = execution.selection
    if plan.operation != "lookup" or not selection.identity_records or selection.suggestions:
        return None
    exact = [
        r for r in selection.identity_records
        if hard_condition_verdict(
            (c.condition.strength, c.verdict)
            for c in selection.checks[r.product.sku_id]
        ) == "match"
    ]
    fields = set(plan.requested_fields) - {"variants", "price_levels", "specifications"}
    if "specifications" in plan.requested_fields or "variants" in plan.requested_fields:
        fields.update(key for r in exact for key in r.product.specs)
    titles = {r.product.sku_id: r.product.title for r in exact}
    facts = []
    for field in sorted(fields) if exact else []:
        values: dict[str, list[int]] = {}
        missing = conflict = 0
        for r in exact:
            if any(i.field == field and i.status == "conflict" for i in r.issues):
                conflict += 1
            elif field not in FACT_FIELDS or (value := field_value(r, field)) is None:
                missing += 1
            else:
                key = next((v for v in values if same_recorded_value(field, v, value)), value)
                values.setdefault(key, []).append(r.product.sku_id)
        facts.append(LookupFieldFacts(
            field=field, values=[
                LookupFieldValue(value=v, versions=[
                    CatalogVersionIdentity(sku_id=sku, title=titles[sku]) for sku in ids
                ])
                for v, ids in values.items()
            ],
            missing_sku_count=missing, conflicted_sku_count=conflict,
        ))
    prices = [r.product.price for r in exact]
    return CatalogLookupSummary(
        exists=True if exact else None if selection.unknown else False,
        matching_complete=selection.unknown == 0,
        matched_sku_count=len(exact), unknown_sku_count=selection.unknown,
        min_price=min(prices) if prices else None, max_price=max(prices) if prices else None,
        price_level_count=len(set(prices)), fields=facts,
        representative_versions=[variant_card(r) for r in representative_records(exact)],
    )


def trace_result(result: CatalogSearchResult) -> None:
    program_event("商品查询返回", outcome=result.status, result_type=result.result_type,
                  reason=result.reason_code, match_kind=result.match_kind,
                  requested_count=result.requested_count, limit=result.limit,
                  limit_capped=result.limit_capped, returned_count=result.returned_count,
                  suggestion_count=len(result.identity_suggestions),
                  returned_ids=[{"spu_id": p.spu_id, "sku_id": p.sku_id}
                                for p in result.products],
                  is_exhaustive=result.is_exhaustive,
                  fact_target=result.facts.target.model_dump() if result.facts else None,
                  aggregated_sku_count=result.facts.sku_count if result.facts else None,
                  missing_fact_sku_count=result.missing_fact_sku_count,
                  conflicted_sku_count=result.conflicted_sku_count,
                  unknown_sku_count=result.unknown_sku_count)


def make_card(
    records: list[CatalogRecord],
    model_records: list[CatalogRecord],
    as_sku: bool,
    requested_fields: list[str],
    *,
    preferred_ranks: dict[int, tuple[int, int]] | None = None,
    representative_preview: bool = False,
) -> SearchProduct:
    records = sorted(records, key=lambda r: (
        (preferred_ranks or {}).get(r.product.sku_id, (0, 0)),
        r.product.price, r.product.sku_id,
    ))
    first = records[0].product
    issues = [issue for r in records for issue in record_issues(r, requested_fields)]
    conflicts = {(issue.sku_id, issue.field) for issue in issues if issue.status == "conflict"}
    common, options = {}, {}
    for key in sorted({key for r in records for key in r.product.specs}):
        values = list(
            dict.fromkeys(
                r.product.specs[key]
                for r in records
                if key in r.product.specs and (r.product.sku_id, key) not in conflicts
            )
        )
        if len(values) == 1 and all(
            key in r.product.specs and (r.product.sku_id, key) not in conflicts for r in records
        ):
            common[key] = values[0]
        elif values:
            options[key] = values
    detail_records = (
        records if as_sku else representative_records(records) if representative_preview
        else records[:MAX_VARIANT_PREVIEW]
    )
    # Compatibility fields always refer to an actually displayed representative.
    if first.sku_id not in {r.product.sku_id for r in detail_records}:
        first = detail_records[0].product
    variants = [
        ProductVariantCard(
            sku_id=r.product.sku_id,
            title=r.product.title,
            price=r.product.price,
            stock=r.product.stock,
            specs=r.product.specs,
            sku_sales_count=r.product.sku_sales_count,
            image_url=r.product.image_url,
        )
        for r in detail_records
    ]
    data = first.model_dump() | {
        "description": next(r.description for r in records if r.product.sku_id == first.sku_id)
        or None,
        "entity_scope": "sku" if as_sku else "spu",
        "representative_sku_id": None if as_sku else first.sku_id,
        "candidate_sku_ids": [r.product.sku_id for r in detail_records],
        "variant_details_complete": len(detail_records) == len(records),
        "model_sku_count": len(model_records),
        "model_min_price": min((r.product.price for r in model_records), default=None),
        "model_max_price": max((r.product.price for r in model_records), default=None),
        "field_issues": issues,
    }
    if not as_sku:
        data.update(
            title=first.spu_title or first.title,
            specs=common,
            series_min_price=min(r.product.price for r in records),
            series_max_price=max(r.product.price for r in records),
            series_total_stock=sum(r.product.stock for r in records),
            series_sku_count=len(records),
            series_common_specs=common,
            series_option_specs=options,
            series_variants=variants,
        )
    return SearchProduct.model_validate(data)


def record_issues(record: CatalogRecord, requested_fields: list[str]) -> list[FieldIssue]:
    issues = list(record.issues)
    for field in dict.fromkeys(requested_fields):
        if field in {"specifications", "variants", "price_levels"}:
            continue
        if field not in FACT_FIELDS or field_value(record, field) is None:
            issues.append(FieldIssue(sku_id=record.product.sku_id, field=field, status="missing"))
    return issues
