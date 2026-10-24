"""Shared scope preparation; tools own their final plans and execution workflows."""

from dataclasses import dataclass
from typing import Any, Literal

from app.core.llm_feedback import OutputValidationError
from app.core.loop_tracing import program_event
from app.repositories.catalog_search import CatalogIdentityRecord, CatalogSearchRepository
from app.rules.product_identity import explicit_product_ids
from app.schemas.catalog_scope import CatalogScopePlan
from app.schemas.catalog_search import (
    CatalogSearchRequest,
    ProductReference,
    SearchPlan,
    SearchScope,
)
from app.tools.catalog.fields import normalize_category_hint
from app.tools.catalog.identity import IdentitySelection, resolve_identity
from app.tools.prompts.catalog_search_specs import build_spec_context
from app.tools.prompts.catalog_workflow import build_scope_prompt
from app.tools.structured_planning import PlanningBudget, generate_tool_plan


@dataclass
class PreparedCatalogScope:
    plan: CatalogScopePlan
    categories: tuple[str, ...]
    identity: IdentitySelection[CatalogIdentityRecord] | None = None

    def payload(self) -> dict:
        return {
            "binding": self.plan.model_dump(mode="json"),
            "categories": list(self.categories),
            "identity_count": len(self.identity.records) if self.identity else 0,
            "identity_preview": [r.product.__dict__ for r in self.identity.records[:20]]
            if self.identity else [],
            "identity_statuses": [r.model_dump(mode="json") for r in self.identity.object_results]
            if self.identity else [],
        }

    def specification_context(self) -> str:
        # Empty selection means common fields only; None means all supported categories.
        categories = (self.categories or None) if self.plan.needs_specifications else ()
        return build_spec_context(categories=categories, include_common_rules=False)

    def search_binding(self) -> dict:
        return self.plan.model_dump(mode="json", include={"category", "search_text", "target"})


async def prepare_catalog_scope(
    model: Any,
    repository: CatalogSearchRepository | None,
    *,
    query: str,
    scope: SearchScope,
    operation: Literal["search", "lookup", "facets"],
    budget: PlanningBudget,
    explicit_category: str | None = None,
    explicit_arguments: dict | None = None,
) -> PreparedCatalogScope:
    def validate(plan: CatalogScopePlan) -> None:
        if plan.category:
            category = normalize_category_hint(plan.category)
            if category is None:
                if plan.disposition != "unsupported":
                    raise OutputValidationError("未知品类须保留原品类并标unsupported。")
            else:
                plan.category = category
        if plan.target and (plan.target.kind, plan.target.id) not in explicit_product_ids(query):
            raise OutputValidationError("编号必须来自原文明示SKU/SPU，不能复制scope或猜编号。")

    draft = await generate_tool_plan(
        model, schema=CatalogScopePlan, required=(), prompt=build_scope_prompt(operation),
        payload={"query": query, "scope": scope.model_dump(mode="json"),
                 "explicit_category": explicit_category,
                 "explicit_arguments": explicit_arguments or {}},
        validate=validate, budget=budget,
    )
    if explicit_category is not None:
        draft.category = explicit_category
    categories = (draft.category,) if draft.category else ()
    identity = None
    if draft.disposition == "ready" and repository is not None:
        if operation == "lookup" and draft.selection == "object":
            plan = SearchPlan(operation="lookup", **{
                key: getattr(draft, key) for key in ("category", "search_text", "target")
            })
            if not plan.target and not plan.search_text:
                ids = scope.sku_ids or scope.spu_ids
                if len(ids) != 1:
                    draft.disposition = "clarification_required"
                    draft.reason = "请明确要查询的一个商品对象。"
                    return PreparedCatalogScope(draft, categories)
                plan.target = ProductReference(kind="sku" if scope.sku_ids else "spu", id=ids[0])
            lookup_scope = (
                SearchScope(**{f"{plan.target.kind}_ids": [plan.target.id]})
                if plan.target else SearchScope()
            )
            # Read identity columns across categories; an explicit category is checked afterwards.
            rows = await repository.load_identity(None, lookup_scope)
            request = CatalogSearchRequest(query=query)
            request.scope = scope
            identity = resolve_identity(request, plan, rows)
            categories = tuple(sorted({r.product.category for r in identity.records}))
        elif draft.needs_specifications and (scope.sku_ids or scope.spu_ids):
            rows = await repository.load_identity(draft.category, scope)
            categories = tuple(sorted({r.product.category for r in rows if r.active}))
    prepared = PreparedCatalogScope(draft, categories, identity)
    program_event("商品范围准备", outcome="prepared", operation=operation,
                  binding=draft.model_dump(mode="json"), categories=list(categories),
                  identity_count=len(identity.records) if identity else None)
    return prepared
