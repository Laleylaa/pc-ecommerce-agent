"""Deterministic search execution: identity -> same-SKU conditions -> rank -> window.

Only this engine chooses candidate pools and ranking. The service owns model planning
and public result mapping; the repository owns SQL and recorded-fact normalization.
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from app.repositories.catalog_search import (
    CatalogIdentityRecord,
    CatalogRecord,
    CatalogSearchRepository,
)
from app.schemas.catalog_search import (
    CatalogSearchRequest,
    ConditionCheck,
    IdentitySuggestion,
    ObjectLookupResult,
    SearchPlan,
    SearchScope,
)
from app.tools.catalog.fields import (
    hard_condition_verdict,
    preference_rank,
)
from app.tools.catalog.identity import IdentitySelection, resolve_identity
from app.tools.catalog.limits import MAX_DISPLAY_COUNT
from app.tools.catalog.matching import alternative_score, check_record


@dataclass
class SearchSelection:
    records: list[CatalogRecord]
    identity_records: list[CatalogRecord]
    object_results: list[ObjectLookupResult]
    checks: dict[int, list[ConditionCheck]]
    unknown: int
    match_kind: Literal["exact", "alternative", "object_facts", "none"]
    fallback_attempted: bool
    identity_match_count: int | None
    suggestions: list[IdentitySuggestion]


@dataclass
class SearchExecution:
    selection: SearchSelection
    page: list[list[CatalogRecord]]
    model_records: list[CatalogRecord]
    preference_scores: dict[int, tuple[int, int]]
    has_preferences: bool
    sku_scope: bool
    total: int
    requested_count: int | None
    offset: int
    limit: int
    next_offset: int | None
    ranking_strategy: str


class CatalogSearchEngine:
    def __init__(self, repository: CatalogSearchRepository):
        self.repository = repository

    async def execute(
        self, request: CatalogSearchRequest, plan: SearchPlan,
        *, identity: IdentitySelection[CatalogIdentityRecord] | None = None,
    ) -> SearchExecution:
        named_object = plan.operation == "lookup" and bool(plan.search_text)
        # ID discovery is independent of caller scope so outside_scope stays distinguishable.
        if plan.target:
            scope = SearchScope(**{f"{plan.target.kind}_ids": [plan.target.id]})
            category = None
        elif named_object:
            scope, category = SearchScope(), plan.category
        else:
            scope, category = request.scope, plan.category
        if identity is None:
            records = await self.repository.load(category, scope)
            identity = resolve_identity(request, plan, records)
        else:
            records = await self.repository.load(
                None, SearchScope(), sku_ids=[r.product.sku_id for r in identity.records],
            )
            identity = replace(identity, records=[r for r in records if r.active])
        return await self._search(request, plan, identity)

    async def _search(
        self, request: CatalogSearchRequest, plan: SearchPlan,
        identity: IdentitySelection[CatalogRecord],
    ) -> SearchExecution:
        identity_rows = identity.records
        object_results, suggestions = identity.object_results, identity.suggestions
        identity_match_count, text_scores = identity.match_count, identity.text_scores
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
        anchored = bool(
            plan.search_text or plan.target or request.scope.sku_ids or request.scope.spu_ids
        )
        fallback_category = plan.category or (
            next(iter(categories)) if anchored and len(categories) == 1 else None
        )
        if (
            identity_rows and not eligible and not unknown
            and plan.matching_conditions and fallback_category
        ):
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
        selection = SearchSelection(
            records=selected, identity_records=identity_rows,
            object_results=object_results, checks=checks,
            unknown=unknown, match_kind=match_kind, fallback_attempted=fallback_attempted,
            identity_match_count=identity_match_count, suggestions=suggestions,
        )
        return await self._window(
            request, plan, selection, scores, text_scores, identity.entity_scope,
        )

    async def _window(
        self, request: CatalogSearchRequest, plan: SearchPlan, selection: SearchSelection,
        scores: dict[int, tuple], text_scores: dict[int, float],
        entity_scope: Literal["sku", "spu"],
    ) -> SearchExecution:
        records, checks = selection.records, selection.checks
        sku_scope = (
            plan.operation == "lookup" and plan.version_view == "list"
        ) or (plan.operation == "search" and plan.candidate_unit == "sku") or entity_scope == "sku"
        if sku_scope:
            groups = [[r] for r in records]
        else:
            by_spu: dict[int, list[CatalogRecord]] = {}
            for r in records:
                by_spu.setdefault(r.product.spu_id, []).append(r)
            groups = list(by_spu.values())

        def sort_key(group: list[CatalogRecord]) -> tuple[Decimal | int | float, int]:
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
            relevance = max((text_scores.get(r.product.sku_id, 0) for r in group), default=0)
            return (-relevance, stable_id)

        has_preferences = any(c.strength == "preference" for c in plan.matching_conditions)
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
        if plan.operation == "lookup" and plan.version_view == "summary":
            requested_count, offset, limit = None, 0, 1
        else:
            requested_count = request.requested_count or request.limit
            cap = request.limit
            offset, limit = request.offset, min(requested_count, cap, MAX_DISPLAY_COUNT)
        total = len(groups)
        page = groups[offset : offset + limit]
        next_offset = offset + len(page)
        model_ids = list(dict.fromkeys(r.product.spu_id for group in page for r in group))
        model_records = (
            await self.repository.load(None, SearchScope(spu_ids=model_ids)) if page else []
        )
        ranking_strategy = ("confirmed_preferences_then_" if has_preferences else "") + (
            "one_dimension_difference_then_" if scores else ""
        ) + (
            "sku_sales" if sku_scope and plan.sort == "sales"
            else "spu_total_sales" if plan.sort == "sales"
            else "multi_field_text_relevance" if plan.sort == "relevance" and plan.search_text
            else plan.sort
        )
        return SearchExecution(
            selection=selection,
            page=page, model_records=model_records, preference_scores=preference_scores,
            has_preferences=has_preferences, sku_scope=bool(sku_scope), total=total,
            requested_count=requested_count, offset=offset, limit=limit,
            next_offset=next_offset if next_offset < total else None,
            ranking_strategy=ranking_strategy,
        )
