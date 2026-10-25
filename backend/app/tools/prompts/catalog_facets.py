"""Facet planning instructions derived from executable catalog field definitions."""

import json

from pydantic import BaseModel

from app.rules.product_identity import PRODUCT_IDENTITY_INTERSECTION_GUIDANCE
from app.tools.prompts.catalog_requirements import PREFERENCES, VALIDATION_BOUNDARY
from app.tools.prompts.catalog_search_specs import build_spec_context
from app.tools.prompts.catalog_workflow import (
    CATALOG_BOUNDARY,
    REQUIREMENT_RULES,
    RESOLVED_SCOPE_RULES,
)
from app.tools.prompts.templates import load_prompt, render_prompt
from app.tools.structured_planning import output_schema


def _domain_context(
    category_hint: str | None,
    *,
    spec_context: str | None = None,
) -> str:
    return render_prompt(
        "catalog/facets.md",
        catalog_boundary=CATALOG_BOUNDARY,
        requirement_rules=REQUIREMENT_RULES,
        resolved_scope_rules=RESOLVED_SCOPE_RULES,
        preferences=PREFERENCES,
        validation_boundary=VALIDATION_BOUNDARY,
        product_identity_intersection_guidance=PRODUCT_IDENTITY_INTERSECTION_GUIDANCE,
        spec_context=spec_context
        if spec_context is not None
        else build_spec_context(category_hint, include_other_categories=False),
    )


def build_facets_review_context(
    category_hint: str | None,
    *,
    spec_context: str | None = None,
) -> str:
    return (
        _domain_context(category_hint, spec_context=spec_context)
        + "\n\n" + load_prompt("catalog/review.md")
    )


def build_facets_prompt(
    schema: type[BaseModel],
    category_hint: str | None,
    *,
    spec_context: str | None = None,
) -> str:
    return render_prompt(
        "catalog/facets_plan.md",
        domain_context=_domain_context(category_hint, spec_context=spec_context),
        output_schema=json.dumps(
            output_schema(schema, schema.model_required_fields), ensure_ascii=False
        ),
    )
