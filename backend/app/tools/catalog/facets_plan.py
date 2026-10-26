"""Facet plan and validation, separate from candidate query orchestration."""
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.schemas.catalog_search import (
    CatalogSearchRequest,
    ProductReference,
    QueryText,
    SearchCondition,
    SearchPlan,
    UnresolvedCatalogRequirement,
)
from app.tools.catalog.fields import CATEGORY_FIELDS, canonical_field, normalize_category_hint
from app.tools.catalog.normalization import (
    ALLOWED_CATEGORIES,
    ALLOWED_FILTERS,
    _canonical_category,
    _normalize_catalog_filters_with_debug,
)
from app.tools.catalog.outcomes import QueryIssue
from app.tools.output_limits import MAX_RESULT_ITEMS
from app.tools.schemas import CatalogFacetInput


class FacetQueryPlan(BaseModel):
    _query_issue: QueryIssue | None = PrivateAttr(default=None)
    _requirement_assessment: dict = PrivateAttr(default_factory=dict)
    model_config = ConfigDict(extra="forbid")
    model_required_fields: ClassVar[tuple[str, ...]] = (
        "selection", "facet", "conditions", "count_scope", "supported",
        "unresolved_requirements",
    )
    model_optional_on_stop: ClassVar[tuple[str, tuple[bool, ...], tuple[str, ...]]] = (
        "supported", (False,), ("facet", "count_scope"),
    )
    selection: Literal["catalog", "object"] = Field(
        default="catalog",
        description="详细规划的结果职责：集合选项统计或误投的对象事实；不是查询范围层级。",
    )
    name: QueryText | None = Field(
        default=None, description="误投目录工具的指定对象名称，保留以返回对应工具边界提示。",
    )
    target: ProductReference | None = SearchPlan.model_fields["target"]
    conditions: list[SearchCondition] = Field(default_factory=list)
    unresolved_requirements: list[UnresolvedCatalogRequirement] = (
        SearchPlan.model_fields["unresolved_requirements"]
    )
    unsupported_requirements: list[str] = Field(default_factory=list)
    clarification_requirements: list[str] = SearchPlan.model_fields["clarification_requirements"]
    # Program-owned business reason, recalculated by capability validation.
    unsupported_code: Literal[
        "request_not_supported", "data_insufficient", "query_needs_clarification",
        "multiple_targets", "conflicting_conditions", "requirement_needs_clarification",
        "filter_data_unavailable",
    ] = Field(
        default="request_not_supported", exclude=True,
    )

    query: str = ""
    facet: Literal["category", "brand", "spec_key", "spec_value"] = "brand"
    category: str | None = None
    brand: str | None = None
    spec_key: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    count_scope: Literal["sku", "spu"] = "spu"
    limit: int = Field(default=MAX_RESULT_ITEMS, ge=1, le=MAX_RESULT_ITEMS)
    supported: bool = Field(default=True, strict=True)
    split_required: bool = Field(
        default=False, strict=True,
        description="请求包含多个独立统计维度，单个facet计划无法交付时为true；程序停止并要求拆分。",
    )
    unsupported_reason: str | None = None
    planner: str = "rule_based"
    normalization_debug: dict = Field(default_factory=dict)


def _validate_catalog_facet_plan(
    plan: FacetQueryPlan, *, request: CatalogFacetInput | None = None,
) -> FacetQueryPlan:
    # Capability checks may reject additional fields, but cannot turn an explicit
    # unsupported result shape back into an executable metadata query.
    category = _canonical_category(plan.category)
    plan.category = category
    plan.filters, normalization_debug = _normalize_catalog_filters_with_debug(
        plan.filters
    )
    if normalization_debug:
        plan.normalization_debug = {
            **plan.normalization_debug,
            **normalization_debug,
        }
    if plan.spec_key:
        plan.spec_key = canonical_field(plan.spec_key)
    for condition in plan.conditions:
        condition.key = canonical_field(condition.key)
    from app.tools.catalog.requirements import requirement_assessment
    from app.tools.catalog.validation import (
        invalid_plan,
        raise_plan_errors,
        validate_plan,
    )

    extra_issues = []
    if plan.supported and plan.selection == "catalog" and (plan.name or plan.target):
        extra_issues.append(invalid_plan(
            "集合统计不执行name/target身份筛选；不得忽略对象扩大范围。"
            "保留原请求，在详细计划中明确是否属于对象事实或其他工具职责。",
            ("selection",), "unhandled_object_scope",
        ))
    if plan.supported and plan.facet == "spec_value" and not plan.spec_key:
        extra_issues.append(invalid_plan(
            "spec_value requires spec_key", ("spec_key",), "missing_spec_key",
        ))

    probe = SearchPlan(
        category=(normalize_category_hint(plan.category) or plan.category) if plan.query else None,
        search_text=plan.name, target=plan.target, conditions=plan.conditions,
        unresolved_requirements=plan.unresolved_requirements,
        clarification_requirements=plan.clarification_requirements,
        unsupported_requirements=plan.unsupported_requirements,
        disposition=(
            "split_required" if plan.split_required else
            "ready" if plan.supported or plan.unresolved_requirements
            or plan.clarification_requirements
            or (plan.selection == "object" and not plan.target and not (
                request and request.scope
            )) else "unsupported"
        ),
        reason=plan.unsupported_reason or "",
    )
    issue = validate_plan(
        probe, CatalogSearchRequest(
            query=plan.query or "structured facet query",
            **({"scope": request.scope} if request and request.scope else {}),
        ), allow_scope_aggregation=True, extra_issues=extra_issues,
    )
    raise_plan_errors(issue)
    plan.target = probe.target
    plan._query_issue = issue
    plan._requirement_assessment = (
        issue.details["requirement_assessment"] if issue else requirement_assessment(probe, [])
    )
    # Keep the model's declaration intact so repeated validation (including
    # cached-plan retries) cannot mistake a previous computed stop for model intent.
    # Execution consumes _query_issue before the declared supported flag.

    if category and category not in ALLOWED_CATEGORIES:
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or (
            f"unsupported category: {category}"
        )
    unknown_filters = sorted(set(plan.filters) - ALLOWED_FILTERS)
    if unknown_filters:
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or (
            "unsupported catalog filters: " + ", ".join(unknown_filters)
        )
    if plan.unsupported_requirements:
        plan.supported = False
        plan.unsupported_reason = (
            plan.unsupported_reason or "; ".join(plan.unsupported_requirements)
        )
    if normalize_category_hint(category):
        allowed = set(CATEGORY_FIELDS[normalize_category_hint(category)].split())
        disallowed_filters = sorted(set(plan.filters) - allowed)
        if disallowed_filters:
            plan.supported = False
            plan.unsupported_reason = plan.unsupported_reason or (
                f"unsupported filters for {category}: "
                + ", ".join(disallowed_filters)
            )
    if plan.min_price is not None and plan.min_price < 0:
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or (
            "min_price cannot be negative"
        )
    if plan.max_price is not None and plan.max_price < 0:
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or (
            "max_price cannot be negative"
        )
    if (
        plan.min_price is not None
        and plan.max_price is not None
        and plan.min_price > plan.max_price
    ):
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or (
            "min_price cannot be greater than max_price"
        )
    return plan
