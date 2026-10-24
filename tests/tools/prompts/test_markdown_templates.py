"""Prompt rendering contracts, actual stage assembly and dynamic capabilities."""

import json
from pathlib import Path

import pytest
from app.schemas.catalog_scope import CatalogScopePlan
from app.schemas.catalog_search import CatalogDiscoveryPlan, CatalogLookupPlan
from app.schemas.tool_plan_review import ToolPlanReview
from app.tools.catalog.compatibility import FacetQueryPlan, _catalog_planner_system_prompt
from app.tools.orders import OrderQueryPlan, _order_planner_system_prompt
from app.tools.prompts.catalog_facets import (
    build_facets_prompt,
    build_facets_review_context,
)
from app.tools.prompts.catalog_search import (
    build_search_prompt,
    build_search_review_context,
)
from app.tools.prompts.catalog_search_specs import build_spec_context
from app.tools.prompts.catalog_workflow import build_scope_prompt
from app.tools.prompts.orders import build_order_prompt
from app.tools.prompts.plan_review import build_review_prompt
from app.tools.prompts.templates import load_prompt, render_prompt
from app.tools.structured_planning import output_schema


def trailing_schema(prompt):
    # Schema is the final output contract, not an earlier example's JSON fragment.
    decoder = json.JSONDecoder()
    for i, ch in enumerate(prompt):
        if ch == "{":
            try:
                value, end = decoder.raw_decode(prompt[i:])
            except ValueError:
                continue
            if not prompt[i + end:].strip():
                return value
    raise AssertionError("No final JSON schema")


@pytest.mark.contract
@pytest.mark.parametrize("operation", ["lookup", "search", "facets"])
def test_scope_prompt_uses_current_schema(operation):
    prompt = build_scope_prompt(operation)
    assert "{{" not in prompt
    assert f"当前工具目的：{operation}" in prompt
    assert trailing_schema(prompt) == CatalogScopePlan.model_json_schema()
    assert "不生成规格条件" in prompt


@pytest.mark.contract
@pytest.mark.parametrize("operation,schema", [
    ("search", CatalogDiscoveryPlan), ("lookup", CatalogLookupPlan),
])
def test_plan_and_review_have_distinct_contracts(operation, schema):
    context = build_spec_context(categories=("显示器",), include_common_rules=False)
    prompt = build_search_prompt(operation=operation, spec_context=context)
    review = build_review_prompt(build_search_review_context(
        operation=operation, spec_context=context,
    ))
    assert trailing_schema(prompt) == output_schema(schema, schema.model_required_fields)
    assert trailing_schema(review) == ToolPlanReview.model_json_schema()
    assert "## 显示器规格" in prompt
    assert "## 键盘规格" not in prompt
    assert "无线充电不能映射为无线连接" in prompt
    assert "不是生成计划" in review
    assert "{{" not in prompt + review


@pytest.mark.contract
@pytest.mark.parametrize("category", [None, "键盘", "显示器"])
def test_facets_dynamic_context_and_output(category):
    prompt = build_facets_prompt(FacetQueryPlan, category)
    review = build_review_prompt(build_facets_review_context(category))
    assert trailing_schema(prompt) == output_schema(
        FacetQueryPlan, FacetQueryPlan.model_required_fields,
    )
    assert trailing_schema(review) == ToolPlanReview.model_json_schema()
    assert "只统计目录或可信候选集合的选项" in prompt
    assert "{{" not in prompt + review


@pytest.mark.contract
def test_order_and_compatibility_entry_points_load_markdown():
    prompt = build_order_prompt(frozenset({"created_at", "order_items"}), OrderQueryPlan)
    assert "created_at, order_items" in prompt
    assert "filters={}" in prompt
    assert trailing_schema(prompt) == output_schema(
        OrderQueryPlan, OrderQueryPlan.model_required_fields,
    )
    assert "purchase_history_match" in _order_planner_system_prompt()
    assert "ProductQueryPlan" in _catalog_planner_system_prompt()


@pytest.mark.contract
def test_render_is_single_pass_and_json_braces_are_literal():
    payload = '{"title":"{{do_not_expand}}", "nested": {"a": 1}}'
    result = render_prompt("shared/plan_review.md", domain_context=payload, output_schema="{}")
    assert payload in result
    assert result.endswith("{}")


@pytest.mark.contract
def test_missing_or_unused_slots_fail_before_model_call():
    with pytest.raises(ValueError, match="missing"):
        render_prompt("shared/plan_review.md", domain_context="x")
    with pytest.raises(ValueError, match="unexpected"):
        render_prompt("shared/plan_review.md", domain_context="x", output_schema="{}", typo="x")
    with pytest.raises(TypeError):
        render_prompt("shared/plan_review.md", domain_context=3, output_schema="{}")


@pytest.mark.contract
@pytest.mark.parametrize("name", ["../orders.py", "/tmp/a.md", "catalog/../../a.md"])
def test_no_arbitrary_resource_paths(name):
    with pytest.raises(ValueError):
        load_prompt(name)


@pytest.mark.contract
def test_missing_template_fails_instead_of_silent_empty_prompt():
    with pytest.raises(FileNotFoundError):
        load_prompt("catalog/not_present.md")


@pytest.mark.contract
def test_template_changes_are_in_evaluation_fingerprint(monkeypatch):
    import importlib

    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "tests/tools/catalog"))
    evidence = importlib.import_module("catalog_evidence")
    state = evidence.code_state()["fingerprints"]
    templates = list((root / "backend/app/tools/prompts/templates").rglob("*.md"))
    assert templates
    assert all(str(p.relative_to(root)) in state for p in templates)


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_first", [False, True])
async def test_order_planner_renders_plan_review_and_optional_repair(malformed_first):
    from types import SimpleNamespace

    from app.tools.orders import LLMOrderQueryPlanner
    from app.tools.schemas import OrderLookupInput

    plan = {"selection": "all", "operation": "list", "requested_fields": ["order_summary"],
            "filters": {}, "unresolved_requirements": []}
    responses = (["not JSON"] if malformed_first else []) + [
        json.dumps(plan), '{"verdict":"approve"}',
    ]

    class Model:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, messages):
            self.calls.append(messages)
            return SimpleNamespace(content=responses.pop(0))

    model = Model()
    result = await LLMOrderQueryPlanner(model).plan(
        OrderLookupInput(user_id=7, query="查一下我的订单"),
    )
    assert result.operation == "list"
    assert result.requested_fields == ["order_summary"]
    assert len(model.calls) == (3 if malformed_first else 2)
    assert model.calls[0][0].content.startswith("# Role and task")
    assert model.calls[-1][0].content.startswith("# 角色与任务")
    if malformed_first:
        feedback = json.loads(model.calls[1][-1].content)
        assert feedback["repair_instruction"] == load_prompt("shared/repair_plan.md")
