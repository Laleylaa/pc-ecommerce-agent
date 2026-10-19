"""Scope ownership, hard/soft execution and requirement clarification, without external I/O."""

import json

import pytest

from app.schemas.catalog_search import CatalogSearchRequest, FieldIssue, SearchScope
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.schemas import CatalogFacetInput
from tests.support.tool_plan_fixtures import facet_plan, search_plan
from tests.support.tool_planning import Model, Repository, product


def soft(key, value, evidence):
    return {
        "key": key,
        "values": [value],
        "evidence": value,
        "strength": "preference",
        "preference_evidence": evidence,
    }


async def search(rows, query, plan, **arguments):
    model = Model(search_plan(plan))
    repository = Repository(rows)
    result = await CatalogSearchService(repository, CatalogSearchPlanner(model)).search(
        CatalogSearchRequest(query=query, **arguments)
    )
    return result, model, repository


@pytest.mark.asyncio
async def test_white_first_then_other_colors_with_budget_sort_and_limit():
    result, model, _ = await search(
        [
            product(2, 2, price="100", color="黑色"),
            product(3, 3, price="250", color="白色"),
            product(4, 4, price="500", color="白色"),
            product(5, 5, price="90"),
            product(1, 1, price="180", color="白色"),
        ],
        "键盘300元以内，白色优先其他颜色也可以，从便宜到贵",
        {
            "category": "键盘",
            "sort": "price_asc",
            "sort_evidence": "从便宜到贵",
            "conditions": [
                soft("color", "白色", "白色优先其他颜色也可以"),
                {"key": "price", "operator": "lte", "values": ["300"], "evidence": "300元以内"},
            ],
        },
        limit=3,
    )
    assert [p.sku_id for p in result.products] == [1, 3, 2]
    assert result.candidate_count == result.total_match_count == 4
    assert result.match_kind == "exact" and not result.fallback_attempted
    assert result.ranking_strategy == "confirmed_preferences_then_price_asc"
    assert result.matching_complete and not result.is_exhaustive
    assert result.products[-1].condition_checks[0].verdict == "mismatch"
    assert len(model.calls) == 1
    # Output validation must still refuse a hard-condition mismatch.
    invalid = result.model_dump()
    invalid["products"][0]["condition_checks"][1]["verdict"] = "mismatch"
    with pytest.raises(ValueError, match="confirmed hard conditions"):
        type(result).model_validate(invalid)


@pytest.mark.asyncio
async def test_no_preference_match_and_missing_preference_do_not_trigger_fallback():
    result, _, _ = await search(
        [product(1, 1), product(2, 2, color="黑色")],
        "键盘最好白色，其他也可以",
        {"category": "键盘", "conditions": [soft("color", "白色", "最好白色，其他也可以")]},
    )
    assert [p.sku_id for p in result.products] == [2, 1]
    assert result.total_match_count == 2 and result.unknown_sku_count == 0
    assert result.match_kind == "exact" and not result.fallback_attempted
    assert result.products[1].condition_checks[0].verdict == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,hard,preferred,actual",
    [
        ("color", "黑色", "白色", "黑色"),
        ("price", "200", "100", "150"),
    ],
)
async def test_soft_condition_does_not_conflict_with_a_hard_condition(key, hard, preferred, actual):
    query = f"键盘{hard}，最好{preferred}"
    row = product(1, price=actual) if key == "price" else product(1, color=actual)
    result, _, _ = await search(
        [row],
        query,
        {
            "category": "键盘",
            "conditions": [
                {
                    "key": key,
                    "operator": "lte" if key == "price" else "eq",
                    "values": [hard],
                    "evidence": hard,
                },
                soft(key, preferred, f"最好{preferred}")
                | ({"operator": "lte"} if key == "price" else {}),
            ],
        },
    )
    assert result.status == "success" and result.products[0].sku_id == 1
    assert [c.verdict for c in result.products[0].condition_checks] == ["match", "mismatch"]


@pytest.mark.asyncio
async def test_preference_ranking_uses_one_sku_and_selects_a_preferred_representative():
    result, _, _ = await search(
        [
            product(1, 1, color="白色", connection_type="有线", price="50"),
            product(2, 1, color="黑色", connection_type="蓝牙", price="40"),
            product(3, 2, color="白色", connection_type="蓝牙", price="200"),
        ],
        "键盘最好白色，最好蓝牙",
        {
            "category": "键盘",
            "conditions": [
                soft("color", "白色", "最好白色"),
                soft("connection_type", "蓝牙", "最好蓝牙"),
            ],
        },
    )
    assert [p.spu_id for p in result.products] == [2, 1]
    # A cheaper nonpreferred version must not represent a preferred version of this model.
    result, _, _ = await search(
        [product(1, 1, color="黑色", price="50"), product(2, 1, color="白色", price="200")],
        "键盘最好白色",
        {"category": "键盘", "conditions": [soft("color", "白色", "最好白色")]},
    )
    assert result.products[0].sku_id == 2
    assert result.products[0].price == 200
    assert set(result.products[0].candidate_sku_ids) == {1, 2}


@pytest.mark.asyncio
async def test_facets_keep_nonpreferred_values_and_rank_before_limit(monkeypatch):
    repository = Repository(
        [
            product(1, 1, color="白色", style="75%"),
            product(2, 2, color="黑色", style="全尺寸"),
            product(3, 3, color="黑色", style="全尺寸"),
        ]
    )
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    query = "键盘有什么配列，白色优先其他颜色也可以"
    plan = {
        "facet": "spec_value",
        "spec_key": "style",
        "category": "keyboard",
        "conditions": [soft("color", "白色", "白色优先其他颜色也可以")],
    }
    service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(Model(facet_plan(plan))))
    result = await service.facets(CatalogFacetInput(query=query, limit=1))
    assert [item.value for item in result.items] == ["75%"]
    assert result.total_value_count == 2 and not result.is_exhaustive
    service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(Model(facet_plan(plan))))
    result = await service.facets(CatalogFacetInput(query=query))
    assert [item.value for item in result.items] == ["75%", "全尺寸"]
    assert result.items[1].sku_count == 2 and result.is_exhaustive


@pytest.mark.asyncio
async def test_excluded_sku_conflict_cannot_pollute_hard_filtered_facets(monkeypatch):
    excluded = product(2, 2, color="黑色", style="全尺寸")
    excluded.issues = [
        FieldIssue(sku_id=2, field="style", status="conflict", values=["75%", "全尺寸"])
    ]
    repository = Repository([product(1, 1, color="白色", style="75%"), excluded])
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    service = CatalogToolService(
        None,
        planner=LLMCatalogQueryPlanner(
            Model(
                facet_plan(
                    {
                        "facet": "spec_value",
                        "spec_key": "style",
                        "category": "keyboard",
                        "conditions": [{"key": "color", "values": ["白色"], "evidence": "白色"}],
                    }
                )
            )
        ),
    )
    result = await service.facets(CatalogFacetInput(query="白色键盘有什么配列"))
    assert [item.value for item in result.items] == ["75%"]
    assert result.is_exhaustive and result.matching_complete and not result.field_issues


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize(
    "case,code",
    [
        ("clarify", "requirement_needs_clarification"),
        ("unsupported", "request_not_supported"),
        ("missing_data", "filter_data_unavailable"),
    ],
)
async def test_business_feedback_is_structured_and_stops(tool, case, code, monkeypatch):
    repository = Repository([])
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    data = {"category": "键盘" if tool == "catalog_search" else "keyboard"}
    query = {
        "clarify": "办公键盘200元以内",
        "unsupported": "键盘必须保证永不损坏",
        "missing_data": "键盘DPI1600",
    }[case]
    if case == "clarify":
        data.update(
            clarification_requirements=["办公"],
            conditions=[
                {"key": "price", "operator": "lte", "values": ["200"], "evidence": "200元以内"},
            ],
        )
    elif case == "unsupported":
        data.update(unsupported_requirements=["保证永不损坏"])
        data.update(
            {"disposition": "unsupported"} if tool == "catalog_search" else {"supported": False}
        )
    else:
        data["conditions"] = [{"key": "max_dpi", "values": ["1600"], "evidence": "1600"}]
    model = Model((search_plan if tool == "catalog_search" else facet_plan)(data))
    handler = (
        CatalogSearchService(repository, CatalogSearchPlanner(model)).search
        if tool == "catalog_search"
        else CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets
    )
    contract = DefaultToolContractProvider().get_contract(tool)
    result = await execute_bound_tool(
        BoundTool(contract, handler), {"query": query}, {"user_id": 7}
    )
    assert result.ok and result.error is None
    output = result.output
    assert output["diagnostics"][0]["code"] == code
    assert output["query_plan"]["conditions"] == search_plan(data)["conditions"]
    if case == "clarify":
        diagnostic = output["diagnostics"][0]
        assert diagnostic["recommended_action"] == "clarify_query"
        assert diagnostic["details"]["requirements"] == ["办公"]
        if tool == "catalog_search":
            assert output["status"] == "clarification_required" and not output["retryable"]
            assert output["query_state"] == "not_executed" and output["total_match_count"] is None
    assert len(model.calls) == 1 and repository.calls == []


@pytest.mark.asyncio
async def test_invalid_clarification_evidence_uses_existing_rule_repair():
    valid = search_plan({"clarification_requirements": ["办公"]})
    model = Model(valid | {"clarification_requirements": ["游戏"]}, valid)
    repository = Repository()
    result = await CatalogSearchService(repository, CatalogSearchPlanner(model)).search(
        CatalogSearchRequest(query="办公键盘")
    )
    assert result.reason_code == "requirement_needs_clarification" and not repository.calls
    error = json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]
    assert error["type"] == "RULE_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,scope,plan,expected",
    [
        ("SKU1价格", None, {}, [1]),
        ("SKU1价格", {"sku_ids": [1]}, {}, [1]),
        (
            "SKU1作参照，找白色键盘",
            None,
            {
                "category": "键盘",
                "conditions": [{"key": "color", "values": ["白色"], "evidence": "白色"}],
            },
            [1, 2, 3],
        ),
        (
            "只在SKU1和SKU2范围里找白色键盘",
            {"sku_ids": [1, 2]},
            {
                "category": "键盘",
                "conditions": [{"key": "color", "values": ["白色"], "evidence": "白色"}],
            },
            [1, 2],
        ),
    ],
)
async def test_formal_graph_preserves_llm2_scope_decision(
    query, scope, plan, expected, monkeypatch
):
    from tests.support.runtime import goal, setup_runtime, task, through_tools

    planned = task(query)
    if scope is not None:
        planned["arguments"]["scope"] = scope
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [planned]}],
        plans=[plan],
    )
    state, _ = await through_tools(runtime, query)
    arguments = executor.calls[0][1]
    assert arguments["query"] == query
    assert arguments.get("scope") == (
        {"sku_ids": [], "spu_ids": []} | scope if scope is not None else None
    )
    output = state["tool_results"][-1]["execution"]["output"]
    assert [p["sku_id"] for p in output["products"]] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_formal_graph_preserves_clarification_for_later_summary(tool, monkeypatch):
    from tests.support.runtime import goal, setup_runtime, task, through_tools

    query = "办公键盘200元以内"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query, tool=tool)]}],
        plans=[{"clarification_requirements": ["办公"]}],
    )
    if tool == "catalog_facets":
        model = Model(facet_plan({"clarification_requirements": ["办公"]}))
        service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(model))

        async def execute(contract, arguments, context):
            return await execute_bound_tool(BoundTool(contract, service.facets), arguments, context)

        executor.execute = execute
    state, _ = await through_tools(runtime, query)
    artifact = next(iter(state["task_artifacts"].values()))
    assert artifact["reason"] == "requirement_needs_clarification"
    assert artifact["value"]["diagnostics"][0]["details"]["requirements"] == ["办公"]
    if tool == "catalog_search":
        assert artifact["value"]["query_state"] == "not_executed"
    else:
        assert not artifact["value"]["matching_complete"]
        assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_facets_internal_model_receives_llm2_scope_without_turning_it_into_target(
    monkeypatch,
):
    repository = Repository([product(1, 1, color="白色", style="75%"), product(2, 2, color="黑色")])
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    model = Model(facet_plan({"selection": "object", "facet": "spec_key"}))
    service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(model))
    result = await service.facets(
        CatalogFacetInput(query="请列出SKU1已记录的规格", scope=SearchScope(sku_ids=[1]))
    )
    payload = json.loads(model.calls[0][1].content)
    assert payload["scope"] == {"sku_ids": [1], "spu_ids": []}
    assert payload["query"] == "请列出SKU1已记录的规格"
    assert result.query_plan["target"] is None
    assert all(value.sku_ids == [1] for item in result.items for value in item.values)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_missing_clarification_decision_is_repaired_before_any_query(tool):
    make_plan = search_plan if tool == "catalog_search" else facet_plan
    valid = make_plan({"clarification_requirements": ["办公"]})
    invalid = make_plan({"clarification_requirements": ["办公"]})
    invalid.pop("clarification_requirements")
    model = Model(invalid, valid)
    request = (
        CatalogSearchRequest(query="办公键盘")
        if tool == "catalog_search"
        else CatalogFacetInput(query="办公键盘")
    )
    planner = (
        CatalogSearchPlanner(model) if tool == "catalog_search" else LLMCatalogQueryPlanner(model)
    )
    plan = await (
        planner.plan(request) if tool == "catalog_search" else planner.plan_facets(request)
    )
    assert plan.clarification_requirements == ["办公"]
    error = json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]
    assert error["type"] == "SCHEMA_ERROR" and error["path"] == ["clarification_requirements"]


@pytest.mark.asyncio
@pytest.mark.parametrize("color", ["白色", None])
async def test_possible_or_matching_conflicts_remain_in_facet_diagnostics(color, monkeypatch):
    record = product(1, 1, style="75%", **({"color": color} if color else {}))
    record.issues = [
        FieldIssue(sku_id=1, field="style", status="conflict", values=["75%", "全尺寸"])
    ]
    repository = Repository([record])
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    model = Model(
        facet_plan(
            {
                "category": "keyboard",
                "facet": "spec_value",
                "spec_key": "style",
                "conditions": [{"key": "color", "values": ["白色"], "evidence": "白色"}],
            }
        )
    )
    result = await CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets(
        CatalogFacetInput(query="白色键盘有什么配列")
    )
    assert result.field_issues[0].field == "style"
    assert not result.is_exhaustive and not result.matching_complete
    assert any(d.code == "data_insufficient" for d in result.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize(
    "case,reason",
    [
        ("unsupported", "request_not_supported"),
        ("missing", "filter_data_unavailable"),
    ],
)
async def test_formal_graph_preserves_capability_and_data_feedback(tool, case, reason, monkeypatch):
    from tests.support.runtime import goal, setup_runtime, task, through_tools

    if case == "unsupported":
        query = "键盘必须保证永不损坏"
        data = {"unsupported_requirements": ["保证永不损坏"]}
        data.update(
            {"disposition": "unsupported"} if tool == "catalog_search" else {"supported": False}
        )
    else:
        query = "键盘DPI1600"
        data = {
            "category": "键盘" if tool == "catalog_search" else "keyboard",
            "conditions": [{"key": "max_dpi", "values": ["1600"], "evidence": "1600"}],
        }
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query, tool=tool)]}],
        plans=[data],
    )
    if tool == "catalog_facets":
        model = Model(facet_plan(data))
        service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(model))

        async def execute(contract, arguments, context):
            return await execute_bound_tool(BoundTool(contract, service.facets), arguments, context)

        executor.execute = execute
    state, _ = await through_tools(runtime, query)
    artifact = next(iter(state["task_artifacts"].values()))
    assert artifact["reason"] == reason and not artifact["usable"]
    assert artifact["value"]["diagnostics"][0]["code"] == reason
    assert artifact["value"]["query_plan"].get("unsupported_requirements", []) == data.get(
        "unsupported_requirements", []
    )
    assert len(state["tool_results"]) == 1
