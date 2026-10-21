
import pytest
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.contracts import BoundTool, execute_bound_tool

from tests.support.tool_plan_fixtures import facet_plan
from tests.support.tool_planning import Model


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
