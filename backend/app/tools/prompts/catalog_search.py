"""Versioned, composable instructions for search intent extraction only."""

import json

from app.rules.product_identity import (
    PRODUCT_IDENTITY_GUIDANCE,
    PRODUCT_IDENTITY_INTERSECTION_GUIDANCE,
)
from app.schemas.catalog_search import CatalogDiscoveryPlan, CatalogLookupPlan
from app.tools.catalog.fields import (
    CATEGORY_FIELDS,
    normalize_category_hint,
)
from app.tools.prompts.catalog_requirements import PREFERENCES, VALIDATION_BOUNDARY
from app.tools.prompts.catalog_search_specs import CONDITION_EXAMPLES, build_spec_context
from app.tools.prompts.catalog_workflow import (
    CATALOG_BOUNDARY,
    REQUIREMENT_RULES,
    RESOLVED_SCOPE_RULES,
)
from app.tools.prompts.templates import load_prompt
from app.tools.structured_planning import output_schema

PROMPT_VERSION = "catalog-query-markdown-v29"


ROLE = load_prompt("catalog/role.md")

CONTEXT = load_prompt("catalog/context.md")

OPERATIONS = {name: load_prompt(f"catalog/{name}.md") for name in ("search", "lookup")}

IDENTITY = load_prompt("catalog/identity.md") + "\n\n" + PRODUCT_IDENTITY_INTERSECTION_GUIDANCE

EXECUTION = load_prompt("catalog/execution.md")

CONDITIONS = load_prompt("catalog/conditions.md")

LIMITS = load_prompt("catalog/limits.md")


def _domain_sections(
    category_hint: str | None,
    operation: str,
    spec_context: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Shared domain contract, without generation role or output instructions."""
    category_hint = normalize_category_hint(category_hint)
    capabilities = json.dumps(
        {"catalog_categories": list(CATEGORY_FIELDS), "spec_context_category_hint": category_hint},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        ("共同边界", CATALOG_BOUNDARY),
        ("共同需求语义", REQUIREMENT_RULES),
        ("阶段交接", RESOLVED_SCOPE_RULES),
        ("请求上下文与任务范围", CONTEXT),
        ("工具职责与返回视图", OPERATIONS[operation]),
        ("职责对照", load_prompt("catalog/intent_examples.md")),
        ("商品身份共同定义", PRODUCT_IDENTITY_GUIDANCE),
        ("名称与可信范围", IDENTITY),
        ("筛选条件与事实字段", CONDITIONS),
        ("硬要求与软偏好", PREFERENCES),
        ("执行职责与业务不变量", EXECUTION),
        ("能力不足与合法停止", LIMITS),
        ("程序与模型职责", VALIDATION_BOUNDARY),
        ("字段能力上下文", capabilities),
        (
            "规格字段定义",
            spec_context
            if spec_context is not None
            else build_spec_context(
                category_hint, include_common_rules=False, include_other_categories=False
            ),
        ),
        ("独立条件示例", CONDITION_EXAMPLES),
    )


def _render(sections: tuple[tuple[str, str], ...]) -> str:
    return "\n\n".join(f"## {title}\n{body}" for title, body in sections)


def build_search_prompt(
    *,
    category_hint: str | None = None,
    operation: str = "search",
    spec_context: str | None = None,
) -> str:
    schema = CatalogLookupPlan if operation == "lookup" else CatalogDiscoveryPlan
    model_schema = output_schema(schema, schema.model_required_fields)
    model_schema["properties"].pop("requested_limit", None)
    output = json.dumps(
        model_schema,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _render(
        (
            ("角色与目标", ROLE),
            *_domain_sections(category_hint, operation, spec_context),
            (
                "生成步骤与输出契约",
                load_prompt("catalog/plan_output.md") + "\n" + output,
            ),
        )
    )


def build_search_review_context(
    *,
    category_hint: str | None = None,
    operation: str = "search",
    spec_context: str | None = None,
) -> str:
    return _render(
        (
            *_domain_sections(category_hint, operation, spec_context),
            (
                "商品计划复核职责",
                load_prompt("catalog/review.md"),
            ),
        )
    )
