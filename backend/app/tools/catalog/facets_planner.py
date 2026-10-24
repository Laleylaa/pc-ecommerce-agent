"""Bounded facet planning using the shared catalog scope workflow."""
import asyncio
import json
from typing import Protocol

from app.core.llm_feedback import OutputValidationError
from app.repositories.catalog_search import CatalogSearchRepository
from app.schemas.catalog_search import SearchScope
from app.tools.catalog.context import _catalog_context_summary
from app.tools.catalog.facets_plan import FacetQueryPlan, _validate_catalog_facet_plan
from app.tools.catalog.fields import explain_conditions
from app.tools.catalog.scope import PreparedCatalogScope, prepare_catalog_scope
from app.tools.schemas import CatalogFacetInput
from app.tools.structured_planning import PlanningBudget, generate_tool_plan


class FacetPlanner(Protocol):
    async def plan_facets(self, request: CatalogFacetInput) -> FacetQueryPlan: ...

class CatalogFacetPlanner:
    def __init__(self, chat_model, *, review: bool = True):
        self.chat_model = chat_model
        self.review = review

    async def plan_facets(self, request: CatalogFacetInput) -> FacetQueryPlan:
        return await self.plan_facets_with_repository(request, None)


    async def plan_facets_with_repository(
        self, request: CatalogFacetInput, repository: CatalogSearchRepository | None,
    ) -> FacetQueryPlan:
        budget = PlanningBudget.start()
        async with asyncio.timeout_at(budget.deadline):
            prepared = await prepare_catalog_scope(
                self.chat_model, repository, query=request.query,
                scope=request.scope or SearchScope(), operation="facets", budget=budget,
                explicit_category=request.category,
                explicit_arguments=request.model_dump(
                    mode="json", include={"facet", "category", "brand", "spec_key",
                                          "filters", "min_price", "max_price"},
                    exclude_unset=True,
                ),
            )
            draft = prepared.plan
            if draft.selection == "object" or draft.disposition != "ready":
                return FacetQueryPlan(
                    query=request.query, limit=request.limit, selection=draft.selection,
                    name=draft.search_text, target=draft.target, category=draft.category,
                    supported=False, unsupported_reason=draft.reason or
                    "指定商品的事实和版本请使用catalog_lookup。",
                    clarification_requirements=[draft.reason or "请明确查询范围。"]
                    if draft.disposition == "clarification_required" else [],
                )
            return await self._plan_facets(request, prepared, budget)


    async def _plan_facets(
        self, request: CatalogFacetInput, prepared: PreparedCatalogScope,
        budget: PlanningBudget,
    ) -> FacetQueryPlan:
        payload = {
            "task": "facets",
            "resolved_scope": prepared.payload(),
            "query": request.query,
            "limit": request.limit,
            "scope": request.scope.model_dump(mode="json") if request.scope else None,
            "explicit_overrides": {
                "facet": request.facet,
                "category": request.category,
                "brand": request.brand,
                "spec_key": request.spec_key,
                "min_price": request.min_price,
                "max_price": request.max_price,
                "filters": request.filters,
            },
            "catalog_context": _catalog_context_summary(request),
        }
        # Only supplied fields are overrides; a default facet is not a user decision.
        payload["explicit_overrides"] = {
            key: value for key, value in payload["explicit_overrides"].items()
            if key in request.model_fields_set
        }
        from app.tools.prompts.catalog_facets import (
            build_facets_prompt,
            build_facets_review_context,
        )

        def validate(plan: FacetQueryPlan) -> dict | None:
            if (
                plan.filters or plan.brand is not None
                or plan.min_price is not None or plan.max_price is not None
            ):
                raise OutputValidationError(
                    "Submit model-generated filters as typed conditions; "
                    "brand, filters and price overrides are reserved for caller-supplied arguments",
                    path=("conditions",),
                )

            _validate_catalog_facet_plan(plan, request=request)
            return {"business_assessment": plan._query_issue.details if plan._query_issue else None,
                    "condition_semantics": explain_conditions(plan.conditions)}

        plan = await generate_tool_plan(
            self.chat_model, schema=FacetQueryPlan, required=FacetQueryPlan.model_required_fields,
            prompt=build_facets_prompt(
                FacetQueryPlan, None, spec_context=prepared.specification_context(),
            ),
            review_context=build_facets_review_context(
                None, spec_context=prepared.specification_context(),
            ),
            payload=json.loads(json.dumps(payload, default=str)), validate=validate,
            review=self.review, budget=budget,
            trusted={
                "query": request.query, "limit": request.limit, "planner": "llm",
                "unsupported_code": "request_not_supported",
                "category": prepared.plan.category, "selection": prepared.plan.selection,
                "name": prepared.plan.search_text,
                "target": prepared.plan.target.model_dump() if prepared.plan.target else None,
            },
        )
        if request.category is not None:
            plan.category = request.category
        if request.brand is not None:
            plan.brand = request.brand
        if request.spec_key is not None:
            plan.spec_key = request.spec_key
        if request.min_price is not None:
            plan.min_price = request.min_price
        if request.max_price is not None:
            plan.max_price = request.max_price
        if request.filters:
            plan.filters = dict(request.filters)
        if not request.query.strip():
            plan.facet = request.facet
        plan.planner = "llm"
        plan.normalization_debug["scope_resolution"] = prepared.payload()
        return plan
