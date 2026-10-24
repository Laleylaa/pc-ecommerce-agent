"""Small shared instructions and a scope-only stage; no database or tool runtime imports."""

import json

from app.schemas.catalog_scope import CatalogScopePlan
from app.tools.catalog.fields import CATEGORY_FIELDS
from app.tools.prompts.templates import load_prompt, render_prompt

CATALOG_BOUNDARY = load_prompt("catalog/boundary.md")

REQUIREMENT_RULES = load_prompt("catalog/requirements.md")

RESOLVED_SCOPE_RULES = load_prompt("catalog/resolved_scope.md")


def build_scope_prompt(operation: str) -> str:
    if operation not in {"lookup", "search", "facets"}:
        raise ValueError("Unknown catalog operation")
    return render_prompt(
        "catalog/scope.md",
        catalog_boundary=CATALOG_BOUNDARY,
        requirement_rules=REQUIREMENT_RULES,
        operation=operation,
        categories="、".join(CATEGORY_FIELDS),
        output_schema=json.dumps(CatalogScopePlan.model_json_schema(), ensure_ascii=False),
    )
