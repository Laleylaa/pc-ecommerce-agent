"""Resolve catalog identities without dropping names or widening caller scopes."""

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from app.repositories.catalog_search import CatalogIdentityRecord, CatalogRecord
from app.schemas.catalog_search import (
    CatalogSearchRequest,
    IdentitySuggestion,
    ObjectLookupResult,
    SearchPlan,
    SearchScope,
)
from app.tools.catalog.fields import matches
from app.tools.catalog.limits import MAX_IDENTITY_SUGGESTIONS
from app.tools.catalog.outcomes import LookupStatus
from app.tools.catalog.text_matching import CatalogTextMatcher, TextMatch

Record = TypeVar("Record", CatalogRecord, CatalogIdentityRecord)


def within_scope(record: Record, scope: SearchScope) -> bool:
    product = record.product
    return (not scope.sku_ids or product.sku_id in scope.sku_ids) and (
        not scope.spu_ids or product.spu_id in scope.spu_ids
    )


def locate_available(
    records: list[Record], scope: SearchScope,
) -> tuple[LookupStatus, list[Record]]:
    in_scope = [record for record in records if within_scope(record, scope)]
    active = [record for record in in_scope if record.active]
    status = (
        "not_found" if not records
        else "outside_scope" if not in_scope
        else "inactive" if not active
        else "found"
    )
    return status, active


def locate_target(
    records: list[Record], scope: SearchScope, name: str | None,
) -> tuple[LookupStatus, list[Record]]:
    """Records were read by explicit ID; a name further constrains that identity.

    Keep absence, availability and caller-scope failures distinct. A conflicting
    name never falls back to the ID alone or starts an unrestricted name search.
    """
    status, active = locate_available(records, scope)
    if status != "found" or not name:
        return status, active
    named = CatalogTextMatcher(records).resolve(name, records)
    if not named:
        return "identity_mismatch", []
    return locate_available(named, scope)


@dataclass
class IdentitySelection(Generic[Record]):
    records: list[Record]
    object_results: list[ObjectLookupResult]
    suggestions: list[IdentitySuggestion]
    text_scores: dict[int, float]
    match_count: int | None
    entity_scope: Literal["sku", "spu"]


def resolve_identity(
    request: CatalogSearchRequest, plan: SearchPlan, records: list[Record],
) -> IdentitySelection[Record]:
    named_object = plan.operation == "lookup" and bool(plan.search_text)
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
            suggestions = identity_suggestions(candidates, "ambiguous" if ambiguous else "similar")
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
    if plan.category:
        category_rows = [r for r in identity_rows if r.product.category == plan.category]
        if identity_rows and not category_rows and plan.operation == "lookup":
            object_results = [r.model_copy(update={"status": "identity_mismatch"})
                              for r in object_results]
        identity_rows = category_rows
    return IdentitySelection(
        identity_rows, object_results, suggestions, text_scores,
        identity_match_count, entity_scope,
    )


def identity_suggestions(
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
