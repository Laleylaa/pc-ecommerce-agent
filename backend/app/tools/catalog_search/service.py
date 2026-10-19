"""Single-goal search: validated plan -> SKU facts -> deterministic result window."""

from decimal import Decimal

from pydantic import ValidationError

from app.core.llm_feedback import OutputValidationError, validation_feedback
from app.repositories.catalog_search import CatalogRecord, CatalogSearchRepository
from app.schemas.catalog import ProductVariantCard
from app.schemas.catalog_search import (
    CatalogSearchRequest,
    CatalogSearchResult,
    FieldIssue,
    ObjectLookupResult,
    SearchDiagnostic,
    SearchPlan,
    SearchProduct,
    SearchScope,
)
from app.tools.catalog_fields import hard_condition_verdict, preference_rank
from app.tools.catalog_search.identity import locate_available, locate_target
from app.tools.catalog_search.identity import within_scope as within_scope
from app.tools.catalog_search.matching import alternative_score, check_record, field_value
from app.tools.catalog_search.outcomes import (
    ResultReason,
    completed_policy,
    data_quality,
    fact_diagnostics,
    policy_for,
)
from app.tools.catalog_search.planner import (
    CatalogSearchPlanner,
    SearchPlanner,
    explicit_lookup,
)
from app.tools.catalog_search.requirements import requirement_assessment
from app.tools.catalog_search.rules import (
    FACT_FIELDS,
    CatalogNameMatcher,
    matches,
)
from app.tools.catalog_search.validation import validate_plan


class CatalogSearchService:
    def __init__(self, repository: CatalogSearchRepository, planner: SearchPlanner | None = None):
        self.repository = repository
        self.planner = planner or CatalogSearchPlanner()

    async def search(self, request: CatalogSearchRequest) -> CatalogSearchResult:
        try:
            direct = explicit_lookup(request.query)
            plan = direct or await self.planner.plan(request)
            plan = SearchPlan.model_validate(plan)
        except (ValidationError, OutputValidationError) as exc:
            return self._stop(
                ResultReason.INVALID_PLAN,
                details={"validation_feedback": validation_feedback(exc)},
            )
        planner_name = "explicit_ids" if direct else type(self.planner).__name__
        issue = validate_plan(plan, request)
        if issue:
            return self._stop(
                issue.reason, issue.message, plan=plan, planner=planner_name, details=issue.details
            )
        # Read the one target independently so outside_scope is not confused with not_found.
        if plan.target:
            scope = SearchScope(**{f"{plan.target.kind}_ids": [plan.target.id]})
            category = None
        elif plan.name:
            # Identity discovery includes unavailable/out-of-scope records. Only
            # in-scope active records may become candidates or matched products.
            scope, category = SearchScope(), plan.category
        else:
            scope, category = request.scope, plan.category
        try:
            records = await self.repository.load(category, scope)
        except ValueError as exc:
            if str(exc) != "catalog_scope_too_large":
                raise
            return self._stop(
                ResultReason.REQUEST_NOT_SUPPORTED, "查询范围过大，请缩小范围。", plan=plan
            )
        object_results = []
        if plan.target:
            status, active = locate_target(records, request.scope, plan.name)
            object_results.append(
                ObjectLookupResult(target=plan.target, name=plan.name, status=status)
            )
            records = active
        elif not plan.name:
            records = [r for r in records if r.active]

        matcher = CatalogNameMatcher(records)
        identity_rows = records
        # A required brand qualifies a named identity. An explicitly preferred
        # brand remains a condition so fallback can offer another same-name model.
        brand_conditions = [
            c for c in plan.conditions if c.key == "brand" and c.strength == "requirement"
        ]
        if plan.name and not plan.target:
            identity_rows = [
                r for r in records if all(matches(r.product.brand, c) for c in brand_conditions)
            ]
            identity_rows = matcher.resolve(plan.name, identity_rows)
            status, identity_rows = locate_available(identity_rows, request.scope)
            object_results.append(ObjectLookupResult(name=plan.name, status=status))
        elif plan.keywords:
            identity_rows = [
                r for r in records if all(matcher.matches(word, r) for word in plan.keywords)
            ]

        checks = {r.product.sku_id: check_record(r, plan) for r in identity_rows}
        eligible, unknown = [], 0
        for record in identity_rows:
            verdict = hard_condition_verdict(
                (c.condition.strength, c.verdict) for c in checks[record.product.sku_id]
            )
            if verdict == "mismatch":
                continue
            if verdict == "unknown":
                unknown += 1
            else:
                eligible.append(record)
        selected = eligible
        match_kind = "exact" if eligible else "none"
        fallback_attempted = False
        scores = {}
        # Explicit query category or retrieved identity/scope facts can establish
        # the category. The hint never establishes a search boundary.
        categories = {r.product.category for r in identity_rows}
        anchored = bool(plan.name or plan.target or request.scope.sku_ids or request.scope.spu_ids)
        fallback_category = plan.category or (
            next(iter(categories)) if anchored and len(categories) == 1 else None
        )
        if not eligible and not unknown and plan.conditions and fallback_category:
            fallback_attempted = True
            selected = []
            for record in identity_rows:
                if record.product.category != fallback_category:
                    continue
                score = alternative_score([
                    c for c in checks[record.product.sku_id]
                    if c.condition.strength == "requirement"
                ])
                if score is not None:
                    selected.append(record)
                    scores[record.product.sku_id] = score
            if selected:
                match_kind = "alternative"
        # A requested object remains useful evidence even if several attached
        # requirements fail or are unknown. It is not advertised as an alternative.
        if (
            not selected
            and identity_rows
            and plan.operation == "lookup"
            and (plan.target or plan.name)
        ):
            selected, match_kind = identity_rows, "object_facts"
        return await self._result(
            request,
            plan,
            selected,
            object_results,
            planner_name,
            unknown=unknown,
            match_kind=match_kind,
            checks=checks,
            scores=scores,
            fallback_attempted=fallback_attempted,
            identity_match_count=len({r.product.spu_id for r in identity_rows})
            if plan.name or plan.target
            else None,
        )

    async def _result(
        self,
        request: CatalogSearchRequest,
        plan: SearchPlan,
        records: list[CatalogRecord],
        object_results: list[ObjectLookupResult],
        planner_name: str,
        *,
        unknown: int = 0,
        match_kind: str = "none",
        checks: dict | None = None,
        scores: dict | None = None,
        fallback_attempted: bool = False,
        identity_match_count: int | None = None,
    ) -> CatalogSearchResult:
        sku_scope = (
            plan.operation == "variants"
            or (plan.target is not None and plan.target.kind == "sku")
            or (plan.name and len(records) == 1 and plan.operation == "lookup")
        )
        if sku_scope:
            groups = [[r] for r in records]
        else:
            by_spu: dict[int, list[CatalogRecord]] = {}
            for r in records:
                by_spu.setdefault(r.product.spu_id, []).append(r)
            groups = list(by_spu.values())

        def sort_key(group: list[CatalogRecord]) -> tuple[Decimal | int, int]:
            first = group[0].product
            stable_id = first.sku_id if sku_scope else first.spu_id
            if plan.sort == "price_asc":
                return (min(r.product.price for r in group), stable_id)
            if plan.sort == "price_desc":
                return (-min(r.product.price for r in group), stable_id)
            if plan.sort == "sales":
                return (-(first.sku_sales_count if sku_scope else first.sales_count), stable_id)
            if plan.sort == "stock":
                return (-sum(r.product.stock for r in group), stable_id)
            return (0, stable_id)

        has_preferences = any(c.strength == "preference" for c in plan.conditions)
        preference_scores = {
            r.product.sku_id: preference_rank(
                (c.condition.strength, c.verdict)
                for c in (checks or {}).get(r.product.sku_id, [])
            )
            for r in records
        }

        def group_sort_key(group: list[CatalogRecord]) -> tuple:
            # Pick one real SKU's preference score; never assemble preferences across versions.
            preference = min(preference_scores[r.product.sku_id] for r in group)
            departure = min(scores[r.product.sku_id] for r in group) if scores else (0, 0)
            return preference, departure, sort_key(group)

        groups.sort(key=group_sort_key)
        total, limit = len(groups), request.limit
        page = groups[request.offset : request.offset + limit]
        next_offset = request.offset + len(page)
        window = {
            "selection_scope": "sku" if sku_scope else "spu",
            "offset": request.offset,
            "limit": limit,
            "returned_count": len(page),
            "next_offset": next_offset if next_offset < total else None,
            "query_plan": plan.model_dump(mode="json")
            | {
                "scope": request.scope.model_dump()
                if request.scope.sku_ids or request.scope.spu_ids
                else None
            },
            "planner": planner_name,
            "result_purpose": plan.purpose,
        }
        model_ids = list(dict.fromkeys(r.product.spu_id for g in page for r in g))
        model_records = (
            await self.repository.load(None, SearchScope(spu_ids=model_ids)) if page else []
        )
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
            )
            card.match_kind = match_kind
            card.condition_checks = [
                c for r in group for c in (checks or {}).get(r.product.sku_id, [])
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
        return CatalogSearchResult(
            **window,
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
            total_match_count=total if match_kind == "exact" else 0,
            matched_spu_count=len({r.product.spu_id for r in records})
            if match_kind == "exact"
            else 0,
            matched_sku_count=len(records) if match_kind == "exact" else 0,
            unknown_sku_count=unknown,
            matching_complete=unknown == 0,
            is_exhaustive=(
                match_kind in {"exact", "none"}
                and unknown == 0
                and request.offset == 0
                and len(cards) == total
            ),
            data_quality=data_quality(conflicted, missing),
            conflicted_sku_count=conflicted,
            missing_fact_sku_count=missing,
            ranking_strategy=("confirmed_preferences_then_" if has_preferences else "")
            + ("one_dimension_difference_then_" if scores else "")
            + (
                "sku_sales"
                if sku_scope and plan.sort == "sales"
                else "spu_total_sales"
                if plan.sort == "sales"
                else plan.sort
            ),
            diagnostics=diagnostics,
        )

    @staticmethod
    def _stop(
        reason: ResultReason,
        message: str | None = None,
        *,
        plan: SearchPlan | None = None,
        planner: str = "",
        details: dict | None = None,
    ) -> CatalogSearchResult:
        policy = policy_for(reason)
        diagnostic = policy.diagnostic(message)
        diagnostic["details"].update(details or {})
        return CatalogSearchResult(
            result_type="empty",
            **policy.fields(),
            query_state="not_executed",
            query_plan=plan.model_dump(mode="json") if plan else {},
            planner=planner,
            result_purpose=plan.purpose if plan else "search",
            diagnostics=[SearchDiagnostic(**diagnostic)],
        )


def make_card(
    records: list[CatalogRecord],
    model_records: list[CatalogRecord],
    as_sku: bool,
    requested_fields: list[str],
    *,
    preferred_ranks: dict[int, tuple[int, int]] | None = None,
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
        for r in records
    ]
    data = first.model_dump() | {
        "description": records[0].description or None,
        "entity_scope": "sku" if as_sku else "spu",
        "representative_sku_id": None if as_sku else first.sku_id,
        "candidate_sku_ids": [r.product.sku_id for r in records],
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
        if field in {"specifications", "variants"}:
            continue
        if field not in FACT_FIELDS or field_value(record, field) is None:
            issues.append(FieldIssue(sku_id=record.product.sku_id, field=field, status="missing"))
    return issues
