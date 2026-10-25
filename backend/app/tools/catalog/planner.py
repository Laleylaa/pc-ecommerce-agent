"""Natural language extraction only. SQL and candidate selection are never model-authored."""

import asyncio
import re
from typing import Literal, Protocol

from app.core.llm_calls import ModelCallError
from app.repositories.catalog_search import CatalogSearchRepository
from app.rules.product_identity import PRODUCT_REFERENCE_PREFIX, product_reference_kind
from app.schemas.catalog_search import (
    CatalogDiscoveryPlan,
    CatalogLookupPlan,
    CatalogSearchRequest,
    ProductReference,
    SearchPlan,
)
from app.tools.catalog.fields import explain_conditions
from app.tools.catalog.scope import PreparedCatalogScope, prepare_catalog_scope
from app.tools.catalog.validation import raise_plan_errors, validate_plan
from app.tools.prompts.catalog_search import build_search_prompt, build_search_review_context
from app.tools.structured_planning import PlanningBudget, generate_tool_plan


class SearchPlanner(Protocol):
    async def plan(self, request: CatalogSearchRequest) -> SearchPlan: ...


def explicit_lookup(query: str) -> SearchPlan | None:
    """Only a closed grammar bypasses the LLM; extra requirements must not disappear."""
    match = re.fullmatch(
        rf"\s*(?:请)?(?:查一下|查询|查看|查)?\s*{PRODUCT_REFERENCE_PREFIX}"
        r"(\d+(?:\s*(?:、|,|，|和|与)\s*\d+)*)\s*"
        r"(?:的)?(参数|详情|价格|库存|有哪些版本|所有版本)?[？?。]?\s*",
        query,
        re.IGNORECASE,
    )
    if not match:
        return None
    kind = product_reference_kind(match[1])
    ids = list(dict.fromkeys(int(value) for value in re.findall(r"\d+", match[2])))
    if len(ids) != 1:
        return SearchPlan(
            operation="lookup", disposition="split_required",
            reason="一次只定位一个商品，请由编排层拆分编号查询。"
        )
    fields = {"价格": "price", "库存": "stock", "参数": "specifications", "详情": "description"}
    versions = match[3] in {"有哪些版本", "所有版本"}
    return SearchPlan(
        operation="lookup",
        version_view="list" if versions else "summary",
        purpose="lookup",
        target=ProductReference(kind=kind, id=ids[0]),
        requested_fields=[fields.get(match[3], "variants" if versions else "specifications")],
    )


class CatalogSearchPlanner:
    def __init__(
        self, chat_model=None, *, review: bool = True,
        operation: Literal["search", "lookup"] = "search",
    ):
        self.chat_model = chat_model
        self.review = review
        self.operation = operation
        self.schema = CatalogLookupPlan if operation == "lookup" else CatalogDiscoveryPlan

    async def plan_with_repository(
        self, request: CatalogSearchRequest, repository: CatalogSearchRepository | None,
    ) -> tuple[SearchPlan, PreparedCatalogScope | None]:
        if self.operation == "lookup" and (direct := explicit_lookup(request.query)):
            return direct, None
        if self.chat_model is None:
            raise OSError("catalog natural-language planner is unavailable")
        budget = PlanningBudget.start()
        planning_timeout = asyncio.timeout_at(budget.deadline)
        try:
            async with planning_timeout:
                prepared = await prepare_catalog_scope(
                    self.chat_model, repository, query=request.query, scope=request.scope,
                    operation=self.operation, budget=budget,
                )
                binding = prepared.search_binding()
                # Identity failure is already an executable business result; no specification LLM.
                if prepared.identity is not None and not prepared.identity.records:
                    return SearchPlan(
                        operation=self.operation, purpose=self.operation, **binding,
                    ), prepared
                return await self._plan(request, prepared, budget), prepared
        except TimeoutError as exc:
            raise ModelCallError(
                "planning_call_timeout" if planning_timeout.expired()
                else "planning_dependency_timeout",
                "Planning did not complete before timeout",
                details={"timeout_source": "local_deadline" if planning_timeout.expired()
                         else "unspecified"},
            ) from exc

    async def plan(self, request: CatalogSearchRequest) -> SearchPlan:
        # Standalone callers have no repository; no database identity is invented.
        plan, _ = await self.plan_with_repository(request, None)
        return plan

    async def _plan(
        self, request: CatalogSearchRequest, prepared: PreparedCatalogScope,
        budget: PlanningBudget,
    ) -> SearchPlan:
        # Context comes from scope preparation, never from the caller hint.
        payload = request.model_dump(mode="json", exclude={"category_hint"})
        payload["resolved_scope"] = prepared.payload()
        payload["limit_source"] = "explicit" if "limit" in request.model_fields_set else "default"

        def validate(plan: SearchPlan) -> dict:
            issue = validate_plan(plan, request, resolved_categories=prepared.categories)
            raise_plan_errors(issue)
            return {"business_assessment": issue.details if issue else None,
                    "condition_semantics": explain_conditions(plan.conditions)}

        return await generate_tool_plan(
            self.chat_model,
            schema=self.schema,
            required=self.schema.model_required_fields,
            prompt=build_search_prompt(
                operation=self.operation, spec_context=prepared.specification_context(),
            ),
            review_context=build_search_review_context(
                operation=self.operation, spec_context=prepared.specification_context(),
            ),
            payload=payload,
            validate=validate,
            review=self.review, budget=budget,
            trusted=prepared.search_binding(),
        )
