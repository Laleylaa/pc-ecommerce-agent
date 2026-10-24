"""Separate requirement limits from invalid plans before reading catalog facts."""

from dataclasses import replace
from decimal import Decimal

from app.core.llm_feedback import OutputErrorType, OutputValidationError, ValidationIssue
from app.rules.product_identity import explicit_product_ids
from app.schemas.catalog_search import (
    CatalogSearchRequest,
    ProductReference,
    SearchCondition,
    SearchPlan,
)
from app.tools.catalog.fields import (
    CATEGORY_ALIASES,
    CATEGORY_FIELDS,
    FACT_FIELDS,
    FIELD_DEFINITIONS,
    FILTER_FIELDS,
    NUMERIC_UNITS,
    equal,
    normalize,
    number,
)
from app.tools.catalog.outcomes import QueryIssue, ResultReason
from app.tools.catalog.requirements import declaration_issues, requirement_assessment


def invalid_plan(message: str, path: tuple = (), code: str = "invalid_plan") -> QueryIssue:
    return QueryIssue(
        ResultReason.INVALID_PLAN,
        message,
        {"phase": "plan_validation", "path": list(path), "error_code": code},
    )


def raise_plan_errors(issue: QueryIssue | None) -> None:
    """Send every deterministic plan violation through the existing repair budget."""
    if issue is None or issue.reason != ResultReason.INVALID_PLAN:
        return
    rows = issue.details.get("issues", []) or [
        {"reason_code": issue.reason.value, "message": issue.message, **issue.details}
    ]
    errors = [
        ValidationIssue(
            OutputErrorType.RULE_ERROR,
            row["message"],
            tuple(row.get("path", [])),
            row.get("error_code", "invalid_plan"),
        )
        for row in rows
        if row["reason_code"] == ResultReason.INVALID_PLAN
    ]
    raise OutputValidationError(issue.message, issues=errors)


def collect_plan_issues(
    plan: SearchPlan,
    request: CatalogSearchRequest,
    *,
    allow_scope_aggregation: bool = False,
) -> list[QueryIssue]:
    # Check executable structure/capability, never certify intent through quoted prose.
    issues: list[QueryIssue] = []
    query_refs = explicit_product_ids(request.query)
    if target := plan.target:
        scope_ids = getattr(request.scope, f"{target.kind}_ids")
        if not query_refs and scope_ids == [target.id]:
            plan.target = None  # Caller scope is already authoritative; never widen it.
        elif (target.kind, target.id) not in query_refs:
            issues.append(invalid_plan("target必须来自原文明确的SKU/SPU编号", ("target",)))
    if plan.category:
        plan.category = CATEGORY_ALIASES.get(plan.category.casefold(), plan.category)
        if plan.category not in CATEGORY_FIELDS:
            issues.append(
                QueryIssue(
                    ResultReason.REQUEST_NOT_SUPPORTED,
                    "当前没有该品类的查询定义。",
                    {"path": ["category"]},
                )
            )
    scope_count = len(set(request.scope.sku_ids or request.scope.spu_ids))
    if plan.disposition == "ready" and not plan.search_text and not plan.target:
        if (
            not allow_scope_aggregation
            and scope_count > 1
            and (
                plan.operation != "search"
                or not (plan.category or plan.conditions or plan.search_text)
            )
        ):
            issues.append(
                QueryIssue(
                    ResultReason.MULTIPLE_TARGETS,
                    "多个范围编号仅用于共同条件搜索，读取资料请按对象拆分。",
                )
            )
        if plan.operation == "lookup":
            if scope_count == 0:
                issues.append(
                    QueryIssue(
                        ResultReason.QUERY_NEEDS_CLARIFICATION, "请明确要查询的一个商品对象。"
                    )
                )
            elif scope_count == 1:
                kind = "sku" if request.scope.sku_ids else "spu"
                plan.target = ProductReference(
                    kind=kind, id=(request.scope.sku_ids or request.scope.spu_ids)[0]
                )
    valid_conditions = []
    for i, c in enumerate(plan.conditions):
        key = c.key
        path = ("conditions", i)
        if key not in FILTER_FIELDS:
            if key in FACT_FIELDS or key in SearchPlan.model_fields:
                issues.append(
                    invalid_plan(
                        f"{key}不是筛选字段；名称检索用search_text，事实询问用requested_fields。"
                        "保留原要求，不删除条件。",
                        (*path, "key"),
                        "invalid_filter_field",
                    )
                )
            else:
                issues.append(
                    QueryIssue(
                        ResultReason.FILTER_DATA_UNAVAILABLE,
                        f"字段体系没有可可靠筛选的定义：{key}。",
                        {"path": list(path), "field": key, "evidence": c.evidence},
                    )
                )
            continue
        if (
            plan.category in CATEGORY_FIELDS
            and plan.category not in FIELD_DEFINITIONS[key].categories
        ):
            issues.append(
                QueryIssue(
                    ResultReason.FILTER_DATA_UNAVAILABLE,
                    f"{plan.category}资料未定义筛选字段：{key}，不能据此判定商品不支持。",
                    {"path": list(path), "field": key, "evidence": c.evidence},
                )
            )
            # Also validate malformed values/operators, independently of capability.
        invalid = False
        if c.operator not in FIELD_DEFINITIONS[key].operators:
            issues.append(
                invalid_plan(
                    f"{key}不能使用操作符{c.operator}；保留原文要求修正计划。",
                    (*path, "operator"),
                    "invalid_operator",
                )
            )
            invalid = True
        if key in NUMERIC_UNITS and any(
            (v := number(key, value)) is None or v < 0 for value in c.values
        ):
            issues.append(
                invalid_plan(
                    f"{key}的数值或单位无法解析；无法表达时保留为未支持要求。",
                    (*path, "values"),
                    "invalid_value",
                )
            )
            invalid = True
        if FIELD_DEFINITIONS[key].kind == "boolean" and any(
            normalize(key, value) not in {"是", "否"} for value in c.values
        ):
            issues.append(
                invalid_plan(
                    f"{key}是布尔字段，values须用是/否，不能删除否定条件。",
                    (*path, "values"),
                    "invalid_value",
                )
            )
            invalid = True
        if not invalid:
            valid_conditions.append(c)
    # Check all independently decidable contradictions; do not stop at the first field.
    issues.extend(_conflicts(plan.model_copy(update={"conditions": valid_conditions})))
    issues.extend(declaration_issues(plan))
    if (
        plan.disposition == "unsupported"
        and not plan.unsupported_requirements
        and not plan.unresolved_requirements
        and not any(
            issue.reason
            in {ResultReason.FILTER_DATA_UNAVAILABLE, ResultReason.REQUEST_NOT_SUPPORTED}
            for issue in issues
        )
    ):
        reason = (
            ResultReason.FILTER_DATA_UNAVAILABLE
            if plan.unsupported_reason
            in {
                ResultReason.DATA_INSUFFICIENT,
                ResultReason.FILTER_DATA_UNAVAILABLE,
            }
            else ResultReason.REQUEST_NOT_SUPPORTED
        )
        issues.append(
            QueryIssue(
                reason,
                plan.reason or "以下要求无法执行：" + "；".join(plan.unsupported_requirements),
                {"requirements": list(plan.unsupported_requirements)},
            )
        )
    if plan.disposition in {"split_required", "clarification_required"} and not (
        plan.disposition == "clarification_required" and plan.clarification_requirements
    ):
        reason = (
            ResultReason.MULTIPLE_TARGETS
            if plan.disposition == "split_required"
            else ResultReason.QUERY_NEEDS_CLARIFICATION
        )
        issues.append(QueryIssue(reason, plan.reason or "需要拆分目标或明确查询对象。"))
    return issues


def validate_plan(
    plan: SearchPlan,
    request: CatalogSearchRequest,
    *,
    allow_scope_aggregation: bool = False,
    extra_issues: list[QueryIssue] | None = None,
    resolved_categories: tuple[str, ...] = (),
) -> QueryIssue | None:
    issues = collect_plan_issues(plan, request, allow_scope_aggregation=allow_scope_aggregation)
    issues.extend(extra_issues or [])
    if plan.operation == "lookup" and not plan.category:
        for condition in plan.conditions:
            definition = FIELD_DEFINITIONS.get(condition.key)
            if definition and any(c not in definition.categories for c in resolved_categories):
                issues.append(QueryIssue(
                    ResultReason.FILTER_DATA_UNAVAILABLE,
                    f"已确认品类未定义{condition.key}筛选能力，不能据此判定商品不支持。",
                    {"field": condition.key, "categories": list(resolved_categories)},
                ))
    if not issues:
        return None
    # Invalid plans must be repaired before trusting any user-facing business limitation.
    primary = next(
        (
            i
            for reason in (ResultReason.INVALID_PLAN, ResultReason.MULTIPLE_TARGETS)
            for i in issues
            if i.reason == reason
        ),
        issues[0],
    )
    rows = [
        {
            "reason_code": i.reason.value,
            "message": i.message,
            "phase": "plan_validation" if i.reason == ResultReason.INVALID_PLAN else "requirements",
            **i.details,
        }
        for i in issues
    ]
    return replace(
        primary,
        details={
            **primary.details,
            "phase": rows[issues.index(primary)]["phase"],
            "issues": rows,
            "requirement_assessment": requirement_assessment(plan, issues),
        },
    )


def _conflicts(plan: SearchPlan) -> list[QueryIssue]:
    issues = []
    bounds: dict[str, list[SearchCondition]] = {}
    for condition in plan.conditions:
        if condition.key in NUMERIC_UNITS and condition.strength == "requirement":
            bounds.setdefault(condition.key, []).append(condition)
    for key, conditions in bounds.items():
        lower, upper = Decimal(0), Decimal("Infinity")
        lower_strict = upper_strict = False
        allowed: set[Decimal] | None = None
        excluded: set[Decimal] = set()
        for c in conditions:
            value = number(key, c.values[0])
            values = {number(key, v) for v in c.values}
            if c.operator in {"eq", "in"}:
                allowed = values if allowed is None else allowed & values
            elif c.operator in {"ne", "not_in"}:
                excluded.update(values)
            if c.operator in {"gte", "gt", "eq"} and value >= lower:
                lower_strict = (lower_strict if value == lower else False) or c.operator == "gt"
                lower = value
            if c.operator in {"lte", "lt", "eq"} and value <= upper:
                upper_strict = (upper_strict if value == upper else False) or c.operator == "lt"
                upper = value
        if lower > upper or (lower == upper and (lower_strict or upper_strict)):
            issues.append(conflicting_conditions(key, conditions))
            continue
        if allowed is None and lower == upper:
            allowed = {lower}
        if allowed is not None and not any(
            value not in excluded
            and (value > lower if lower_strict else value >= lower)
            and (value < upper if upper_strict else value <= upper)
            for value in allowed
        ):
            issues.append(conflicting_conditions(key, conditions))
    required_conditions = [c for c in plan.conditions if c.strength == "requirement"]
    for key in {c.key for c in required_conditions} - NUMERIC_UNITS.keys():
        includes = [
            {normalize(key, value) for value in c.values}
            for c in required_conditions
            if c.key == key and c.operator in {"eq", "in"}
        ]
        excludes = {
            normalize(key, value)
            for c in required_conditions
            if c.key == key and c.operator in {"ne", "not_in"}
            for value in c.values
        }
        candidates = set.union(*includes) if includes else set()
        # A recorded composite value may satisfy several different requirements.
        # Query literals alone are not the universe of possible recorded values.
        if definition := FIELD_DEFINITIONS.get(key):
            candidates.update(normalize(key, v) for v in definition.value_groups)
            candidates.update(
                normalize(key, v)
                for group in definition.value_groups.values()
                for v in group
            )
        if includes and not any(
            all(any(equal(key, candidate, value) for value in group) for group in includes)
            and not any(equal(key, candidate, value) for value in excludes)
            for candidate in candidates
        ):
            issues.append(
                conflicting_conditions(key, [c for c in required_conditions if c.key == key])
            )
    return issues


def conflicting_conditions(key: str, conditions: list[SearchCondition]) -> QueryIssue:
    return QueryIssue(
        ResultReason.CONFLICTING_CONDITIONS,
        f"{key} 的条件无法同时成立。",
        {"field": key, "conditions": [c.model_dump(mode="json") for c in conditions]},
    )
