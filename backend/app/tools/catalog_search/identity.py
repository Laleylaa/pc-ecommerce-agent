"""Resolve catalog identities without dropping names or widening caller scopes."""

from app.repositories.catalog_search import CatalogRecord
from app.schemas.catalog_search import SearchScope
from app.tools.catalog_search.outcomes import LookupStatus
from app.tools.catalog_search.rules import CatalogNameMatcher


def within_scope(record: CatalogRecord, scope: SearchScope) -> bool:
    product = record.product
    return (not scope.sku_ids or product.sku_id in scope.sku_ids) and (
        not scope.spu_ids or product.spu_id in scope.spu_ids
    )


def locate_available(
    records: list[CatalogRecord], scope: SearchScope,
) -> tuple[LookupStatus, list[CatalogRecord]]:
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
    records: list[CatalogRecord], scope: SearchScope, name: str | None,
) -> tuple[LookupStatus, list[CatalogRecord]]:
    """Records were read by explicit ID; a name further constrains that identity.

    Keep absence, availability and caller-scope failures distinct. A conflicting
    name never falls back to the ID alone or starts an unrestricted name search.
    """
    status, active = locate_available(records, scope)
    if status != "found" or not name:
        return status, active
    named = CatalogNameMatcher(records).resolve(name, records)
    if not named:
        return "identity_mismatch", []
    return locate_available(named, scope)
