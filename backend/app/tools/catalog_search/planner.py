"""Natural language extraction only. SQL and candidate selection are never model-authored."""

import re
from typing import Literal, Protocol

from app.rules.product_identity import PRODUCT_REFERENCE_PREFIX, product_reference_kind
from app.schemas.catalog_search import (
    CatalogDiscoveryPlan,
    CatalogLookupPlan,
    CatalogSearchRequest,
    ProductReference,
    SearchPlan,
)
from app.tools.catalog_fields import explain_conditions
from app.tools.catalog_search.validation import raise_plan_errors, validate_plan
from app.tools.prompts.catalog_search import build_search_prompt, build_search_review_context
from app.tools.structured_planning import generate_tool_plan


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

    async def plan(self, request: CatalogSearchRequest) -> SearchPlan:
        if self.operation == "lookup" and (direct := explicit_lookup(request.query)):
            return direct
        if self.chat_model is None:
            raise OSError("catalog natural-language planner is unavailable")
        # The hint routes system context; it is not user intent or a hard filter.
        payload = request.model_dump(mode="json", exclude={"category_hint"})
        payload["limit_source"] = "explicit" if "limit" in request.model_fields_set else "default"

        def validate(plan: SearchPlan) -> dict:
            issue = validate_plan(plan, request)
            raise_plan_errors(issue)
            return {"business_assessment": issue.details if issue else None,
                    "condition_semantics": explain_conditions(plan.conditions)}

        return await generate_tool_plan(
            self.chat_model,
            schema=self.schema,
            required=SearchPlan.model_required_fields,
            prompt=build_search_prompt(
                category_hint=request.category_hint, operation=self.operation,
            ),
            review_context=build_search_review_context(
                category_hint=request.category_hint, operation=self.operation,
            ),
            payload=payload,
            validate=validate,
            review=self.review,
        )
