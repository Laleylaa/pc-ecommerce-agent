"""Two-stage policy checks without real models, embeddings or configured databases."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

from app.agent.decisions import PlannedToolCall
from app.agent.goal_planning import (
    ExecutionPlan,
    GoalPlan,
    PlanValidationError,
    context_objects,
    execution_definition,
    generate_plan,
    plan_tools,
    route_goals,
    validate_execution,
)
from app.agent.graph import AgentRuntime
from app.agent.routing import RequestRoutePlan, RoutedSubquery
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from tests.support.tool_plan_fixtures import order_plan


def goal(query="查询键盘", disposition="tool_planning", identity="goal_1"):
    return {"id": identity, "query": query, "source_text": query, "disposition": disposition}


def task(tool="catalog_search", **arguments):
    return {
        "goal_id": "goal_1",
        "id": "task_1",
        "tool_name": tool,
        "arguments": {"query": "查询", **arguments},
    }


def route():
    return RequestRoutePlan(
        rewritten_query="查询",
        subqueries=[
            RoutedSubquery(
                **goal("查询"),
                reason_code="test",
                planning_pending=True,
            )
        ],
    )


class SequenceModel:
    def __init__(self, name, outputs):
        self.name, self.outputs, self.calls = name, iter(outputs), []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        value = next(self.outputs)
        if isinstance(value, Exception):
            raise value
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.name,
                    "args": value,
                    "id": "call",
                }
            ],
        )


@pytest.mark.asyncio
async def test_llm1_owns_mixed_history_and_current_fact_decisions():
    previous, current = "总结刚才的库存", "查最新的键盘库存"
    model = SequenceModel(
        "route_request",
        [
            {
                "rewritten_query": previous + current,
                "goals": [
                    goal(previous, "session_grounded_response"),
                    goal(current, identity="goal_2"),
                ],
            }
        ],
    )
    state = {
        "message": previous + current,
        "history": [{"role": "assistant", "content": "之前查询时库存为2件。"}],
    }
    await AgentRuntime._request_route(SimpleNamespace(request_router=model), state)
    assert "planning_error" not in state
    assert [g["disposition"] for g in state["route_plan"]["subqueries"]] == [
        "session_grounded_response",
        "tool_planning",
    ]
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_historical_order_summary_is_not_forced_back_to_order_lookup():
    query = "把刚才查到的订单分别列一下"
    model = SequenceModel(
        "route_request",
        [
            {
                "rewritten_query": query,
                "goals": [goal(query, "session_grounded_response")],
            }
        ],
    )
    state = {"message": query, "history": [{"role": "assistant", "content": "之前有两笔订单。"}]}
    await AgentRuntime._request_route(SimpleNamespace(request_router=model), state)
    assert state["route_plan"]["subqueries"][0]["disposition"] == "session_grounded_response"
    assert state["route_plan"]["subqueries"][0]["tasks"] == []


@pytest.mark.asyncio
async def test_internal_route_adapter_accepts_query_allowed_by_tool_schema():
    planned = task("knowledge_search", query="轴体知识" * 600)
    model = SequenceModel("submit_execution_plan", [{"tasks": [planned]}])
    state = {"route_plan": route().model_dump()}
    await plan_tools(
        SimpleNamespace(task_planner=model, contract_provider=DefaultToolContractProvider()), state
    )
    assert len(model.calls) == 1
    assert (
        state["route_plan"]["subqueries"][0]["tasks"][0]["canonical_query"]
        == (planned["arguments"]["query"])
    )


@pytest.mark.asyncio
async def test_unavailable_history_is_repaired_by_llm1_instead_of_reclassified_by_program():
    query = "总结刚才的回答"
    invalid = {"rewritten_query": query, "goals": [goal(query, "session_grounded_response")]}
    corrected = {
        "rewritten_query": query,
        "goals": [
            {
                **goal(query, "clarification"),
                "missing_information": ["需要总结的历史内容"],
            }
        ],
    }
    model = SequenceModel("route_request", [invalid, corrected])
    state = {"message": query, "history": []}
    result = await route_goals(SimpleNamespace(request_router=model), state)
    assert result.subqueries[0].disposition == "clarification"
    assert len(model.calls) == 2
    assert "no historical answer" in model.calls[1][-1].content


@pytest.mark.asyncio
async def test_planning_payloads_exclude_old_runtime_controls_and_unrelated_context():
    memory = {
        "catalog": {
            "query_plan": {"query": "白色键盘", "limit": 20, "planner": "old"},
            "search_page": {"query": "白色键盘", "offset": 50, "next_offset": 70, "limit": 20},
            "candidate_sku_ids": [12],
        }
    }
    first = SequenceModel("route_request", [{"rewritten_query": "查询", "goals": [goal("查询")]}])
    second = SequenceModel("submit_execution_plan", [{"tasks": [task()]}])
    runtime = SimpleNamespace(
        request_router=first, task_planner=second, contract_provider=DefaultToolContractProvider()
    )
    state = {"message": "查询", "working_memory_snapshot": memory}
    state["route_plan"] = (await route_goals(runtime, state)).model_dump()
    await plan_tools(runtime, state)
    first_payload = json.loads(first.calls[0][-1].content)
    assert first_payload["prior_queries"] == {"catalog": "白色键盘"}
    assert "working_memory" not in first_payload
    assert "catalog_page" not in context_objects(state)
    assert "offset" not in json.dumps(first_payload)
    second_payload = json.loads(second.calls[0][-1].content)
    assert second_payload["context_objects"] == {}
    assert set(second_payload["goals"][0]) == {"id", "query", "source_text", "context_refs"}
    schema = execution_definition(runtime.contract_provider)["function"]["parameters"]
    assert "page_from" not in json.dumps(schema)
    assert "offset" not in json.dumps(schema)
    search = next(t for t in second_payload["tools"] if t["function"]["name"] == "catalog_search")
    assert "offset" not in search["function"]["parameters"]["properties"]


@pytest.mark.parametrize("old_field", ["page_from", "offset", "page"])
def test_old_position_controls_are_rejected(old_field):
    for tool in ("catalog_search", "order_lookup"):
        data = {"tasks": [task(tool)]}
        if old_field == "page_from":
            data["tasks"][0][old_field] = "catalog_page"
        else:
            data["tasks"][0]["arguments"][old_field] = 2
        with pytest.raises(ValidationError):
            validate_execution(
                ExecutionPlan.model_validate(data), route(), DefaultToolContractProvider(), {}
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,default,explicit",
    [
        ("catalog_search", 3, 2),
        ("catalog_facets", 20, 4),
        ("order_lookup", 5, 2),
        ("knowledge_search", 3, 1),
        ("policy_search", 3, 4),
    ],
)
async def test_limit_survives_planning_runtime_and_formal_binding(tool, default, explicit):
    provider = DefaultToolContractProvider()

    class Receipt(BaseModel):
        limit: int

    async def handler(request):
        return Receipt(limit=request.limit)

    for value in (None, explicit):
        planned = task(tool, **({"limit": value} if value is not None else {}))
        plan = ExecutionPlan.model_validate({"tasks": [planned]})
        validate_execution(plan, route(), provider, {})
        state = {"message": "查询", "execution_plan": plan.model_dump()}
        runtime = SimpleNamespace(contract_provider=provider)
        call, _ = AgentRuntime._prepare_tool_call(
            runtime,
            state,
            PlannedToolCall(
                id="call",
                name=tool,
                subquery="task_1",
                arguments=plan.tasks[0].arguments,
            ),
        )
        assert ("limit" in call.arguments) == (value is not None)
        contract = provider.get_contract(tool).model_copy(update={"output_model": Receipt})
        result = await execute_bound_tool(
            BoundTool(contract, handler), call.arguments, {"user_id": 7}
        )
        assert result.ok and result.output["limit"] == (default if value is None else value)


@pytest.mark.parametrize("value", [True, "2", 2.5, 0, -1, 999])
@pytest.mark.parametrize(
    "tool",
    [
        "catalog_search",
        "catalog_facets",
        "order_lookup",
        "knowledge_search",
        "policy_search",
    ],
)
def test_limit_types_and_ranges_are_strict(value, tool):
    with pytest.raises(ValidationError):
        validate_execution(
            ExecutionPlan.model_validate({"tasks": [task(tool, limit=value)]}),
            route(),
            DefaultToolContractProvider(),
            {},
        )


@pytest.mark.asyncio
async def test_llm2_repairs_argument_errors_once_preserving_quantity():
    invalid = {"tasks": [task(limit="2")]}
    valid = {"tasks": [task(limit=2)]}
    model = SequenceModel("submit_execution_plan", [invalid, valid])
    state = {"route_plan": route().model_dump()}
    await plan_tools(
        SimpleNamespace(task_planner=model, contract_provider=DefaultToolContractProvider()), state
    )
    assert len(model.calls) == 2
    assert state["execution_plan"]["tasks"][0]["arguments"]["limit"] == 2
    feedback = json.loads(model.calls[1][-1].content)
    assert feedback["validation_feedback"]["errors"][0]["code"] == "int_type"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["llm1", "llm2"])
@pytest.mark.parametrize(
    "error",
    [
        ValueError("program defect"),
        TypeError("program defect"),
        PermissionError("not allowed"),
        OSError("dependency down"),
    ],
)
async def test_non_contract_failures_are_not_retried(stage, error):
    name = "route_request" if stage == "llm1" else "submit_execution_plan"
    schema = GoalPlan if stage == "llm1" else ExecutionPlan
    data = (
        {"rewritten_query": "查询", "goals": [goal("查询")]}
        if stage == "llm1"
        else {
            "tasks": [task()],
        }
    )
    model = SequenceModel(name, [data])

    def broken_validator(plan):
        raise error

    with pytest.raises(type(error), match=str(error)):
        await generate_plan(model, name, schema, "test", {}, broken_validator, lambda: None)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_model_transport_failure_is_not_a_format_repair():
    model = SequenceModel("route_request", [OSError("network unavailable")])
    with pytest.raises(OSError, match="network unavailable"):
        await route_goals(SimpleNamespace(request_router=model), {"message": "查询"})
    assert len(model.calls) == 1


def test_multiple_order_positions_are_rejected_before_execution():
    first = task("order_lookup")
    second = {
        **task("order_lookup"),
        "id": "task_2",
        "depends_on": ["task_1"],
        "bindings": [
            {"source": "task_output", "ref": "task_1", "kind": "order", "positions": [1, 2]}
        ],
    }
    with pytest.raises(PlanValidationError, match="one order"):
        validate_execution(
            ExecutionPlan.model_validate({"tasks": [first, second]}),
            route(),
            DefaultToolContractProvider(),
            {},
        )


@pytest.mark.asyncio
async def test_missing_reference_is_a_bounded_contract_error():
    model = SequenceModel(
        "route_request",
        [
            {
                "rewritten_query": "查询",
                "goals": [{**goal("查询"), "context_refs": ["missing"]}],
            }
        ]
        * 2,
    )
    with pytest.raises(PlanValidationError, match="unavailable objects"):
        await route_goals(SimpleNamespace(request_router=model), {"message": "查询"})
    assert len(model.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["knowledge_search", "policy_search"])
async def test_real_document_tool_returns_at_most_requested_evidence(monkeypatch, tool):
    import httpx

    from app.tools import knowledge
    from app.tools.web_search import WebSearchToolService

    monkeypatch.setattr(knowledge, "_load_vector_index", lambda *args: None)
    service = knowledge.KnowledgeRetrievalToolService(
        documents=[
            knowledge.LocalKnowledgeDocument(
                id=i,
                title=f"测试材料{i}",
                document_type="policy",
                content="查询测试内容",
            )
            for i in range(5)
        ],
        embedding_provider=SimpleNamespace(model_name="fixture"),
    )
    web = WebSearchToolService("fixture", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": [
            {"title": f"材料{i}", "url": f"https://example.com/{i}",
             "content": "事实", "score": 1}
            for i in range(5)
        ]}),
    ))
    handler = service.search_policy if tool == "policy_search" else web.search
    contract = DefaultToolContractProvider().get_contract(tool)
    for arguments, expected in [
        ({"query": "查询"}, 3),
        ({"query": "查询", "limit": 1}, 1),
        ({"query": "查询", "limit": 4}, 4),
    ]:
        result = await execute_bound_tool(BoundTool(contract, handler), arguments, {"user_id": 7})
        assert result.ok and len(result.output["documents"]) == expected


@pytest.mark.asyncio
async def test_real_order_tool_uses_caller_limit_instead_of_reparsing_query(monkeypatch):
    from app.repositories.orders import OrderSearchPage
    from app.tools import orders

    calls = []

    class Repository:
        def __init__(self, session):
            pass

        async def list_recent_orders_page(self, user_id, *, limit, offset):
            calls.append((user_id, limit, offset))
            return OrderSearchPage(orders=[], total_count=0, offset=offset)

    monkeypatch.setattr(orders, "OrderRepository", Repository)
    class Planner:
        async def plan(self, request):
            return orders.OrderQueryPlan(selection="recent", operation="list",
                                         requested_fields=["order_summary"])

    service = orders.OrderToolService(None, planner=Planner())
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    for args, expected in [
        ({"query": "最近两笔订单"}, 5),
        ({"query": "最近五笔订单", "limit": 2}, 2),
    ]:
        result = await execute_bound_tool(BoundTool(contract, service.lookup), args, {"user_id": 7})
        assert result.ok and calls[-1] == (7, expected, 0)


@pytest.mark.asyncio
async def test_order_internal_llm_cannot_override_caller_quantity():
    from app.tools.orders import LLMOrderQueryPlanner
    from app.tools.schemas import OrderLookupInput
    from tests.support.tool_planning import scripted_review

    class Model:
        async def ainvoke(self, messages):
            if assessment := scripted_review(messages):
                return assessment
            return AIMessage(
                content=json.dumps(
                    order_plan({
                        "selection": "recent", "operation": "list",
                        "requested_fields": ["order_summary"], "limit": 5,
                    })
                )
            )

    result = await LLMOrderQueryPlanner(Model()).plan(
        OrderLookupInput(
            user_id=7,
            query="最近两笔订单",
            limit=2,
        )
    )
    assert result.limit == 2
