"""Both catalog tools preserve the distinction between limits, bad plans and facts."""

import json

import pytest
from app.schemas.catalog_search import CatalogSearchRequest, SearchPlan
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.catalog_search.validation import validate_plan
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool

from tests.support.tool_plan_fixtures import facet_plan, search_plan
from tests.support.tool_planning import Model, Repository, product


async def invoke(tool, monkeypatch, query, *plans, repository=None):
    repository = repository or Repository([product(1, color="白色")])
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    fixture = search_plan if tool == "catalog_search" else facet_plan

    class PlanningModel(Model):
        async def ainvoke(self, messages):
            assert not repository.calls, "Query ran before planning/repair finished"
            return await super().ainvoke(messages)

    model = PlanningModel(*(fixture(p) if isinstance(p, dict) else p for p in plans))
    handler = (
        CatalogSearchService(repository, CatalogSearchPlanner(model)).search
        if tool == "catalog_search"
        else CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets
    )
    result = await execute_bound_tool(
        BoundTool(DefaultToolContractProvider().get_contract(tool), handler),
        {"query": query},
        {"user_id": 7},
    )
    return result, model, repository


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_all_requirement_blockers_survive_and_partial_query_never_runs(tool, monkeypatch):
    plan = {
        "conditions": [
            {"key": "price", "operator": "lte", "values": ["200"], "evidence": "200元以内"},
            {"key": "video_input", "values": ["USB-C"], "evidence": "USB-C视频输入"},
        ],
        "clarification_requirements": ["办公"],
        "unsupported_requirements": ["终身不坏"],
    }
    result, model, repository = await invoke(
        tool,
        monkeypatch,
        "办公，200元以内，USB-C视频输入，还要终身不坏",
        plan,
    )
    assert result.ok and not repository.calls and len(model.calls) == 1
    detail = result.output["diagnostics"][0]["details"]
    assert detail["phase"] == "requirements"
    assert {i["reason_code"] for i in detail["issues"]} == {
        "filter_data_unavailable",
        "requirement_needs_clarification",
        "request_not_supported",
    }
    assessment = detail["requirement_assessment"]
    assert assessment["scope"] == "extracted_requirements" and assessment["coverage"] == "partial"
    assert [i["state"] for i in assessment["items"]] == [
        "executable",
        "blocked",
        "blocked",
        "blocked",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize("blocked", [True, False])
async def test_no_supported_requirements_differs_from_no_filter_requirements(
    tool,
    blocked,
    monkeypatch,
):
    plan = {"clarification_requirements": ["办公"]} if blocked else {"category": "键盘"}
    result, model, repository = await invoke(
        tool,
        monkeypatch,
        "办公" if blocked else "键盘",
        plan,
    )
    assert result.ok and len(model.calls) == 1
    details = next(
        d["details"]
        for d in result.output["diagnostics"]
        if "requirement_assessment" in d["details"]
    )
    assert details["requirement_assessment"]["coverage"] == (
        "none" if blocked else "not_applicable"
    )
    assert bool(repository.calls) is not blocked


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_title_misuse_and_operator_errors_repair_together_before_query(tool, monkeypatch):
    invalid = {
        "conditions": [
            {"key": "title", "values": ["K01"], "evidence": "K01"},
            {"key": "color", "operator": "gt", "values": ["白色"], "evidence": "白色"},
        ]
    }
    valid = {
        "name": "K01",
        "conditions": [
            {"key": "color", "values": ["白色"], "evidence": "白色"},
        ],
    }
    result, model, repository = await invoke(tool, monkeypatch, "K01白色", invalid, valid)
    assert result.ok and len(model.calls) == 2 and repository.calls
    feedback = json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"]
    assert {e["code"] for e in feedback} == {"invalid_filter_field", "invalid_operator"}
    assert {tuple(e["path"]) for e in feedback} == {
        ("conditions", 0, "key"),
        ("conditions", 1, "operator"),
    }
    assert model.calls[0][1].content == model.calls[1][1].content
    assert result.output["query_plan"]["conditions"][0]["values"] == ["白色"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_invalid_source_has_priority_even_when_model_declares_unsupported(tool, monkeypatch):
    plan = {
        "unsupported_requirements": ["凭空要求"],
        "conditions": [
            {"key": "price", "values": ["200瓦"], "evidence": "200"},
        ],
    }
    plan.update(
        {"disposition": "unsupported"} if tool == "catalog_search" else {"supported": False}
    )
    result, model, repository = await invoke(tool, monkeypatch, "200元", plan, plan)
    assert not result.ok and not repository.calls and len(model.calls) == 2
    assert result.error.phase == "plan_validation"
    assert result.error.code == "planning_output_invalid" and not result.error.retryable
    errors = result.error.details["validation_feedback"]["errors"]
    assert {e["code"] for e in errors} == {"invalid_evidence", "invalid_value"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize("bad,kind", [("not JSON", "FORMAT_ERROR"), ({}, "SCHEMA_ERROR")])
async def test_exhausted_format_and_schema_failures_keep_subtypes(tool, bad, kind, monkeypatch):
    # Empty objects must stay empty instead of being expanded by the valid-plan fixture.
    response = json.dumps(bad) if isinstance(bad, dict) else bad
    result, model, repository = await invoke(tool, monkeypatch, "找商品", response, response)
    assert not result.ok and not repository.calls and len(model.calls) == 2
    assert result.error.phase == "plan_validation"
    assert {e["type"] for e in result.error.details["validation_feedback"]["errors"]} == {kind}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_missing_record_values_are_query_results_not_missing_field_definitions(
    tool, monkeypatch
):
    result, _, repository = await invoke(
        tool,
        monkeypatch,
        "蓝牙键盘",
        {
            "category": "键盘",
            "conditions": [
                {"key": "connection_type", "values": ["蓝牙"], "evidence": "蓝牙"},
            ],
        },
        repository=Repository([product(1)]),
    )
    assert result.ok and len(repository.calls) == 1
    diagnostic = next(d for d in result.output["diagnostics"] if d["code"] == "data_insufficient")
    assert diagnostic["details"]["phase"] == "query_result"
    assert not result.output["matching_complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize("fault", [OSError("unavailable"), ValueError("program defect")])
async def test_execution_faults_never_become_business_no_match(tool, fault, monkeypatch):
    class FailingRepository(Repository):
        async def load(self, category, scope):
            raise fault

    result, model, _ = await invoke(
        tool,
        monkeypatch,
        "键盘",
        {"category": "键盘"},
        repository=FailingRepository(),
    )
    assert len(model.calls) == 1 and not result.ok and result.output is None
    assert result.error.phase == "execution"
    assert result.error.code == (
        "dependency_unavailable" if isinstance(fault, OSError) else "execution_error"
    )


@pytest.mark.asyncio
async def test_facet_missing_dimension_is_plan_error_even_with_business_blocker(monkeypatch):
    invalid = {"facet": "spec_value", "clarification_requirements": ["办公"]}
    result, model, repository = await invoke(
        "catalog_facets",
        monkeypatch,
        "办公键盘有哪些配列",
        invalid,
        invalid,
    )
    assert not result.ok and len(model.calls) == 2 and not repository.calls
    assert result.error.details["validation_feedback"]["errors"][0]["code"] == "missing_spec_key"


@pytest.mark.asyncio
async def test_unknown_facet_dimension_keeps_requirement_phase_after_revalidation(monkeypatch):
    result, model, repository = await invoke(
        "catalog_facets",
        monkeypatch,
        "显示器有哪些USB-C视频输入选项",
        {"facet": "spec_value", "spec_key": "video_input", "category": "显示器"},
    )
    assert result.ok and len(model.calls) == 1 and not repository.calls
    diagnostic = result.output["diagnostics"][0]
    assert diagnostic["code"] == "filter_data_unavailable"
    assert diagnostic["details"]["phase"] == "requirements"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_genuine_conflict_stops_without_model_repair(tool, monkeypatch):
    result, model, repository = await invoke(
        tool,
        monkeypatch,
        "价格低于100但高于200",
        {
            "conditions": [
                {"key": "price", "operator": "lt", "values": ["100"], "evidence": "低于100"},
                {"key": "price", "operator": "gt", "values": ["200"], "evidence": "高于200"},
            ],
        },
    )
    assert result.ok and len(model.calls) == 1 and not repository.calls
    diagnostic = result.output["diagnostics"][0]
    assert diagnostic["code"] == "conflicting_conditions"
    assert diagnostic["details"]["phase"] == "requirements"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_output_contract_failure_is_distinct_from_handler_failure(tool):
    async def malformed(request):
        return {"items": []}

    result = await execute_bound_tool(
        BoundTool(DefaultToolContractProvider().get_contract(tool), malformed),
        {"query": "键盘"},
        {"user_id": 7},
    )
    assert not result.ok and result.error.phase == "execution"
    assert result.error.details["failure_type"] == "output_contract_error"


@pytest.mark.asyncio
async def test_historical_title_filter_repairs_into_keywords_or_stops(monkeypatch):
    invalid = {
        "conditions": [
            {"key": "title", "values": ["机械键盘"], "evidence": "机械键盘"},
        ]
    }
    for repair in ({"keywords": ["机械键盘"]}, invalid):
        result, model, repository = await invoke(
            "catalog_search",
            monkeypatch,
            "机械键盘",
            invalid,
            repair,
        )
        assert len(model.calls) == 2
        if repair is invalid:
            assert not result.ok and not repository.calls
            assert result.error.phase == "plan_validation"
            assert result.error.code == "planning_output_invalid"
        else:
            assert result.ok and repository.calls
            assert result.output["query_plan"]["keywords"] == ["机械键盘"]


@pytest.mark.asyncio
async def test_facet_category_alias_keeps_source_through_revalidation(monkeypatch):
    result, model, repository = await invoke(
        "catalog_facets",
        monkeypatch,
        "Mice brands",
        {"category": "mice"},
        repository=Repository([product(1, category="鼠标")]),
    )
    assert result.ok and len(model.calls) == 1 and repository.calls
    assert result.output["items"][0]["value"] == "Test"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "lookup", "variants"])
async def test_split_disposition_does_not_require_a_single_target(operation, monkeypatch):
    result, model, repository = await invoke(
        "catalog_search",
        monkeypatch,
        "帮我比较Keychron 星轴 K01和Logitech 影刃 M01",
        {"operation": operation, "disposition": "split_required", "purpose": "lookup"},
    )
    assert result.ok and len(model.calls) == 1 and not repository.calls
    output = result.output
    assert output["status"] == "split_required"
    assert output["reason_code"] == "multiple_targets"
    assert output["recommended_action"] == "split_tasks"
    assert output["query_state"] == "not_executed" and output["total_match_count"] is None
    assert {i["reason_code"] for i in output["diagnostics"][0]["details"]["issues"]} == {
        "multiple_targets",
    }


def test_multi_scope_lookup_is_split_without_claiming_identity_is_missing():
    issue = validate_plan(
        SearchPlan(operation="lookup"),
        CatalogSearchRequest(query="查看这些商品的价格", scope={"sku_ids": [1, 2]}),
    )
    assert issue.reason == "multiple_targets"
    assert len(issue.details["issues"]) == 1


def test_split_preserves_other_problems_and_invalid_plan_still_has_priority():
    plan = SearchPlan.model_validate(
        search_plan(
            {
                "operation": "lookup",
                "disposition": "split_required",
                "conditions": [{"key": "video_input", "values": ["USB-C"], "evidence": "USB-C"}],
            }
        )
    )
    request = CatalogSearchRequest(query="比较A和B，都要USB-C")
    issue = validate_plan(plan, request)
    assert issue.reason == "multiple_targets"
    assert {i["reason_code"] for i in issue.details["issues"]} == {
        "multiple_targets",
        "filter_data_unavailable",
    }
    plan.conditions[0].evidence = "凭空生成"
    assert validate_plan(plan, request).reason == "invalid_plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize(
    "field,value,source",
    [
        ("video_input", "USB-C", "USB-C视频输入"),
        ("wireless_charging", "是", "无线充电"),
        ("battery_life_h", "100小时", "续航100小时"),
    ],
)
async def test_one_requirement_owns_condition_and_unsupported_declarations(
    tool,
    field,
    value,
    source,
    monkeypatch,
):
    plan = {
        "conditions": [
            {"key": "price", "operator": "lte", "values": ["2000"], "evidence": "2000元以内"},
            {"key": field, "values": [value], "evidence": source},
        ],
        "unsupported_requirements": [source, source],
    }
    plan.update(
        {"disposition": "unsupported"} if tool == "catalog_search" else {"supported": False}
    )
    result, model, repository = await invoke(
        tool,
        monkeypatch,
        f"2000元以内，必须{source}",
        plan,
    )
    assert result.ok and len(model.calls) == 1 and not repository.calls
    detail = result.output["diagnostics"][0]["details"]
    assert len(detail["issues"]) == 1
    assert detail["issues"][0]["reason_code"] == "filter_data_unavailable"
    assessment = detail["requirement_assessment"]
    assert assessment["coverage"] == "partial" and len(assessment["items"]) == 2
    requirement = assessment["items"][1]
    assert requirement["id"] == "requirement_2"
    assert requirement["source"] == source and requirement["state"] == "blocked"
    assert requirement["reasons"] == ["filter_data_unavailable"]
    assert requirement["source_paths"] == [
        ["conditions", 1],
        ["unsupported_requirements", 0],
        ["unsupported_requirements", 1],
    ]
    assert len(result.output["query_plan"]["conditions"]) == 2


def test_shared_evidence_keeps_all_predicates_and_does_not_merge_distinct_requirements():
    plan = SearchPlan.model_validate(
        search_plan(
            {
                "conditions": [
                    {
                        "key": "price",
                        "operator": "gte",
                        "values": ["100"],
                        "evidence": "100到200元",
                    },
                    {
                        "key": "price",
                        "operator": "lte",
                        "values": ["200"],
                        "evidence": "100到200元",
                    },
                ],
                "clarification_requirements": ["办公", "办公"],
                "unsupported_requirements": ["防水", "防水但不要防尘"],
            }
        )
    )
    issue = validate_plan(plan, CatalogSearchRequest(query="100到200元，办公，防水但不要防尘"))
    items = issue.details["requirement_assessment"]["items"]
    assert [i["source"] for i in items] == ["100到200元", "办公", "防水", "防水但不要防尘"]
    assert items[0]["source_paths"] == [["conditions", 0], ["conditions", 1]]
    assert items[0]["state"] == "executable" and len(plan.conditions) == 2
    assert all(i["state"] == "blocked" for i in items[1:])


def test_supported_predicate_cannot_silently_discard_an_unsupported_declaration():
    plan = SearchPlan.model_validate(
        search_plan(
            {
                "conditions": [{"key": "color", "values": ["白色"], "evidence": "白色"}],
                "unsupported_requirements": ["白色"],
            }
        )
    )
    issue = validate_plan(plan, CatalogSearchRequest(query="白色键盘"))
    assert issue.reason == "request_not_supported"
    assessment = issue.details["requirement_assessment"]
    assert assessment["coverage"] == "none" and len(assessment["items"]) == 1
    assert assessment["items"][0]["state"] == "blocked"
