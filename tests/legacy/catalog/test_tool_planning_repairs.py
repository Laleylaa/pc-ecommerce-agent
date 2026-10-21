import asyncio

import pytest
from app.tools.catalog import (
    CatalogToolService,
    LLMCatalogQueryPlanner,
)
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.contracts import BoundTool, execute_bound_tool
from app.tools.schemas import (
    CatalogFacetOutput,
    ToolDiagnostic,
)

from tests.support.tool_plan_fixtures import search_plan
from tests.support.tool_planning import (
    Model,
    Repository,
    conditions,
    product,
)


@pytest.mark.asyncio
async def test_formal_orchestrator_can_forward_number_query_unchanged(monkeypatch):
    from tests.support.runtime import goal, setup_runtime, task, through_tools

    query = "编号1有什么规格"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query, tool="catalog_facets")]}],
    )
    model = Model()
    service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(model))

    async def execute(contract, arguments, context):
        assert arguments["query"] == query
        return await execute_bound_tool(BoundTool(contract, service.facets), arguments, context)

    executor.execute = execute
    state, _ = await through_tools(runtime, query)
    result = state["tool_results"][-1]["execution"]
    assert result["ok"] and result["output"]["query_plan"]["target"] == {"kind": "sku", "id": 1}
    assert model.calls == []


@pytest.mark.asyncio
async def test_formal_graph_does_not_replan_legal_arguments_after_serialization_failure(
    monkeypatch,
):
    from tests.support.runtime import goal, setup_runtime, task, through_tools

    query = "有哪些键盘品牌"
    runtime, planner, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query, tool="catalog_facets")]}],
    )
    attempts = []

    async def handler(request):
        attempts.append(request)
        return CatalogFacetOutput(
            result_type="empty",
            facet="brand",
            diagnostics=[ToolDiagnostic(code="test", message="test", details={"bad": object()})],
        )

    async def execute(contract, arguments, context):
        return await execute_bound_tool(BoundTool(contract, handler), arguments, context)

    executor.execute = execute
    state, _ = await through_tools(runtime, query)
    error = state["tool_results"][-1]["execution"]["error"]
    assert error["code"] == "execution_error" and error["recommended_action"] == "stop"
    assert len(attempts) == 1 and len(planner.calls["submit_execution_plan"]) == 1


@pytest.mark.asyncio
async def test_formal_execution_timeout_reuses_validated_plan_and_reloads_facts(monkeypatch):
    from tests.support.runtime import goal, setup_runtime, task, through_tools

    query = "白色蓝牙键盘200元以内"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query)]}],
    )
    model = Model(search_plan({"category": "键盘", "conditions": conditions()}), search_plan())

    class SlowFirstRepository(Repository):
        async def load(self, category, scope):
            if not self.calls:
                self.calls.append((category, scope))
                await asyncio.sleep(0.05)
            return await super().load(category, scope)

    repository = SlowFirstRepository(
        [
            product(1, color="白色", connection_type="蓝牙"),
            product(2, color="白色", connection_type="蓝牙", price="599"),
        ]
    )
    service = CatalogSearchService(repository, CatalogSearchPlanner(model))

    async def execute(contract, arguments, context):
        bound = BoundTool(contract.model_copy(update={"timeout_seconds": 0.01}), service.search)
        return await execute_bound_tool(bound, arguments, context)

    executor.execute = execute
    state, _ = await through_tools(runtime, query)
    assert len(model.calls) == 1 and len(repository.calls) == 3
    assert len(state["tool_waves"][0]["calls"]) == 2
    output = state["tool_results"][-1]["execution"]["output"]
    assert output["products"][0]["candidate_sku_ids"] == [1]
    assert output["total_match_count"] == 1


@pytest.mark.asyncio
async def test_formal_internal_plan_exhaustion_does_not_restart_or_replan_arguments(monkeypatch):
    from tests.support.runtime import goal, setup_runtime, task, through_tools

    query = "白色蓝牙键盘200元以内"
    runtime, planner, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query)]}],
    )
    model, repository = Model({}, {}), Repository()
    service = CatalogSearchService(repository, CatalogSearchPlanner(model))
    attempts = []

    async def execute(contract, arguments, context):
        attempts.append(arguments)
        return await execute_bound_tool(BoundTool(contract, service.search), arguments, context)

    executor.execute = execute
    state, _ = await through_tools(runtime, query)
    error = state["tool_results"][-1]["execution"]["error"]
    assert error["code"] == "planning_output_invalid" and error["recommended_action"] == "stop"
    assert len(model.calls) == 2 and len(attempts) == 1 and repository.calls == []
    assert len(planner.calls["submit_execution_plan"]) == 1
