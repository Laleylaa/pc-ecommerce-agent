"""Same-SKU evidence and bounded fallback. Never reinterpret user language."""

from decimal import Decimal

from app.repositories.catalog_search import CatalogRecord
from app.schemas.catalog_search import ConditionCheck, SearchPlan
from app.tools.catalog.fields import matches, number


def field_value(record: CatalogRecord, field: str) -> str | None:
    if field in {"brand", "price", "stock", "title", "category", "sales_count", "sku_sales_count"}:
        return str(getattr(record.product, field))
    if field == "availability":
        return "是" if record.product.stock > 0 else "否"
    if field == "description":
        return record.description or None
    return record.product.specs.get(field)


def check_record(record: CatalogRecord, plan: SearchPlan) -> list[ConditionCheck]:
    conflicts = {issue.field for issue in record.issues if issue.status == "conflict"}
    checks = []
    for condition in plan.conditions:
        actual = field_value(record, condition.key)
        decision = None if condition.key in conflicts else matches(actual, condition)
        checks.append(
            ConditionCheck(
                sku_id=record.product.sku_id,
                condition=condition,
                actual=actual,
                verdict="unknown" if decision is None else "match" if decision else "mismatch",
            )
        )
    return checks


def alternative_score(checks: list[ConditionCheck]) -> tuple[int, Decimal] | None:
    """At most one known mismatching field, all remaining predicates proven true.

    An interval's two endpoints are one dimension. Unknown facts never qualify.
    Preference failures precede requirement failures; relative numeric departure
    is a tie-breaker, not a claim that unlike specifications are interchangeable.
    """
    if any(check.verdict == "unknown" for check in checks):
        return None
    failed = [check for check in checks if check.verdict == "mismatch"]
    if len({check.condition.key for check in failed}) != 1:
        return None
    # Category is an anchor, even when represented as a structured condition.
    if failed[0].condition.key == "category":
        return None
    requirement_cost = int(any(c.condition.strength == "requirement" for c in failed))
    departures = []
    for check in failed:
        value = number(check.condition.key, check.actual or "")
        boundaries = [number(check.condition.key, v) for v in check.condition.values]
        if value is None or any(v is None for v in boundaries):
            departures.append(Decimal(1))
        else:
            departures.append(min(abs(value - v) / max(abs(v), Decimal(1)) for v in boundaries))
    return requirement_cost, max(departures)
