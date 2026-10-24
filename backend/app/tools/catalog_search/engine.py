"""Deterministic search execution: identity -> same-SKU conditions -> rank -> window.

Only this engine chooses candidate pools and ranking. The service owns model planning
and public result mapping; the repository owns SQL and recorded-fact normalization.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, Literal, TypeVar

from app.core.loop_tracing import program_event
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
from app.tools.catalog_fields import (
    FIELD_DEFINITIONS,
    hard_condition_verdict,
    matches,
    preference_rank,
)
from app.tools.catalog_search.identity import locate_available, locate_target, within_scope
from app.tools.catalog_search.limits import MAX_DISPLAY_COUNT, MAX_IDENTITY_SUGGESTIONS
from app.tools.catalog_search.matching import alternative_score, check_record
from app.tools.catalog_search.text_matching import CatalogTextMatcher, TextMatch

Record = TypeVar("Record", CatalogRecord, CatalogIdentityRecord)


@dataclass
class IdentitySelection(Generic[Record]):
    records: list[Record]
    object_results: list[ObjectLookupResult]
    suggestions: list[IdentitySuggestion]
    text_scores: dict[int, float]
    match_count: int | None
    entity_scope: Literal["sku", "spu"]


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


class UnsupportedCatalogFilter(ValueError):
    """A resolved category cannot execute a requested filter; no record verdict is implied."""

    def __init__(self, category: str, field: str):
        self.category, self.field = category, field
        super().__init__(f"{category}未定义{field}筛选能力，不能据此判定商品不支持。")


class CatalogSearchEngine:
    def __init__(self, repository: CatalogSearchRepository):
        self.repository = repository

    async def execute(
        self, request: CatalogSearchRequest, plan: SearchPlan,
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
        records = await self.repository.load(category, scope)
        identity = self._resolve(request, plan, records)
        if plan.operation == "lookup":
            for category in {r.product.category for r in identity.records}:
                for condition in plan.conditions:
                    definition = FIELD_DEFINITIONS[condition.key]
                    if category not in definition.categories:
                        raise UnsupportedCatalogFilter(category, condition.key)
        return await self._search(request, plan, identity)

    def _resolve(
        self, request: CatalogSearchRequest, plan: SearchPlan, records: list[Record],
    ) -> IdentitySelection[Record]:
        named_object = plan.operation == "lookup" and bool(plan.search_text)
        loaded_count = len(records)
        object_results: list[ObjectLookupResult] = []
        suggestions: list[IdentitySuggestion] = []
        text_scores: dict[int, float] = {}
        identity_rows = records
        identity_match_count = None
        if plan.target:
            status, identity_rows = locate_target(records, request.scope, plan.search_text)
            identity_match_count = len({r.product.spu_id for r in identity_rows})
            object_results.append(ObjectLookupResult(
                target=plan.target, name=plan.search_text, status=status,
            ))
        elif plan.search_text:
            # Brand is an explicit identity constraint only when it is a hard requirement.
            brand_conditions = [
                c for c in plan.conditions
                if named_object and c.key == "brand" and c.strength == "requirement"
            ]
            identity_pool = [
                r for r in records if all(matches(r.product.brand, c) for c in brand_conditions)
            ]
            matcher = CatalogTextMatcher(records)
            hits = matcher.rank(plan.search_text, identity_pool)
            text_scores = {hit.record.product.sku_id: hit.score for hit in hits}
            # Exact title priority only applies to object lookup, not general discovery.
            confirmed = (
                matcher.resolve(plan.search_text, identity_pool) if named_object
                else [hit.record for hit in hits if hit.confirmed]
            )
            status, identity_rows = locate_available(confirmed, request.scope)
            identity_match_count = len({r.product.spu_id for r in identity_rows})
            ambiguous = named_object and len({r.product.spu_id for r in identity_rows}) > 1
            if ambiguous or not confirmed:
                eligible_ids = {r.product.sku_id for r in identity_rows}
                candidates = [
                    hit for hit in hits
                    if hit.record.active and within_scope(hit.record, request.scope)
                    and (
                        hit.record.product.sku_id in eligible_ids
                        if ambiguous else not hit.confirmed
                    )
                ]
                suggestions = self._suggestions(candidates, "ambiguous" if ambiguous else "similar")
                if suggestions:
                    status = "ambiguous" if ambiguous else "similar"
                    identity_rows = []
            if named_object or suggestions:
                object_results.append(ObjectLookupResult(name=plan.search_text, status=status))
        else:
            identity_rows = [r for r in records if r.active]

        # Establish identity BEFORE filtering. A one-SKU remainder is not a SKU identity.
        entity_scope = "spu"
        if plan.target:
            entity_scope = plan.target.kind
        elif len(request.scope.sku_ids) == 1:
            entity_scope = "sku"
        elif named_object and len(identity_rows) == 1:
            matcher = CatalogTextMatcher(records)
            if not matcher.matches_model(plan.search_text, identity_rows[0]):
                entity_scope = "sku"
        program_event("对象定位", outcome="completed", loaded_sku_count=loaded_count,
                      entity_scope=entity_scope,
                      identity_sku_count=len(identity_rows),
                      confirmed_spu_count=identity_match_count, suggestion_count=len(suggestions),
                      object_statuses=[item.status for item in object_results])
        if plan.category:
            identity_rows = [r for r in identity_rows if r.product.category == plan.category]
        return IdentitySelection(
            identity_rows, object_results, suggestions, text_scores,
            identity_match_count, entity_scope,
        )

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
            and plan.conditions and fallback_category
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
        program_event("条件筛选",
                      outcome="skipped_identity_pending" if suggestions else "completed",
                      identity_sku_count=len(identity_rows),
                      exact_sku_count=len(eligible), unknown_sku_count=unknown,
                      mismatch_sku_count=len(identity_rows) - len(eligible) - unknown,
                      selected_sku_count=len(selected), match_kind=match_kind,
                      fallback_attempted=fallback_attempted)
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
        ) or entity_scope == "sku"
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
        if plan.operation == "lookup" and plan.version_view == "summary":
            requested_count, offset, limit = None, 0, 1
        else:
            requested_count = (
                request.limit if "limit" in request.model_fields_set
                else plan.requested_limit or request.limit
            )
            offset, limit = request.offset, min(requested_count, MAX_DISPLAY_COUNT)
        total = len(groups)
        page = groups[offset : offset + limit]
        next_offset = offset + len(page)
        program_event("排序与返回窗口", outcome="selected", sort=plan.sort,
                      preference_priority=has_preferences, alternative_priority=bool(scores),
                      selection_scope="sku" if sku_scope else "spu", total_count=total,
                      offset=offset, requested_count=requested_count, limit=limit,
                      window_applied=plan.operation == "search" or plan.version_view == "list",
                      limit_capped=requested_count is not None and requested_count > limit,
                      returned_count=len(page))
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

    @staticmethod
    def _suggestions(
        hits: list[TextMatch], match_type: Literal["ambiguous", "similar"],
    ) -> list[IdentitySuggestion]:
        selected: dict[int, IdentitySuggestion] = {}
        for hit in hits:
            product = hit.record.product
            if product.spu_id not in selected:
                selected[product.spu_id] = IdentitySuggestion(
                    spu_id=product.spu_id, title=product.spu_title or product.title,
                    brand=product.brand, category=product.category, match_type=match_type,
                    score=round(hit.score, 4), matched_terms=list(hit.matched_terms),
                    unmatched_terms=list(hit.unmatched_terms),
                )
            if len(selected) >= MAX_IDENTITY_SUGGESTIONS:
                break
        return list(selected.values())
