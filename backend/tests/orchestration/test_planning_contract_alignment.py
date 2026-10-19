"""Contract regressions with scripted models and in-memory data only."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.agent.decisions import PlannedToolCall
from app.agent.goal_planning import (
    ExecutionPlan,
    Goal,
    GoalPlan,
    bind_arguments,
    plan_tools,
    route_goals,
    validate_execution,
    validate_goal_identity,
)
from app.agent.graph import AgentRuntime
from app.agent.routing import RequestRoutePlan, RoutedSubquery
from app.models import OrderInfo
from app.repositories import orders as order_repository
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.orders import _extract_order_id
from app.tools.schemas import OrderLookupInput, OrderLookupOutput


def route(query, *, source_text=None, refs=()):
    return RequestRoutePlan(
        rewritten_query=query,
        subqueries=[
            RoutedSubquery(
                id="goal_1",
                query=query,
                source_text=source_text or query,
                disposition="tool_planning",
                reason_code="test",
                planning_pending=True,
                context_refs=list(refs),
            )
        ],
    )


def execution(tool, arguments, **kwargs):
    return ExecutionPlan.model_validate(
        {
            "tasks": [
                {
                    "id": "task_1",
                    "goal_id": "goal_1",
                    "tool_name": tool,
                    "arguments": arguments,
                    **kwargs,
                }
            ]
        }
    )


def test_internal_route_does_not_add_identifier_length_constraints():
    goal_id = "goal_" + "1" * 25
    task_id = "task_" + "1" * 25
    goal_plan = GoalPlan(
        rewritten_query="查询",
        goals=[
            {
                "id": goal_id,
                "query": "查询",
                "source_text": "查询",
                "disposition": "tool_planning",
            }
        ],
    )
    plan = ExecutionPlan.model_validate(
        {
            "tasks": [
                {
                    "id": task_id,
                    "goal_id": goal_id,
                    "tool_name": "catalog_search",
                    "arguments": {"query": "查询"},
                }
            ]
        }
    )
    routed = RequestRoutePlan(
        rewritten_query="查询",
        subqueries=[
            {
                **goal_plan.goals[0].model_dump(),
                "reason_code": "test",
                "tasks": [
                    {
                        "id": task_id,
                        "goal_id": goal_id,
                        "canonical_query": "查询",
                        "capability": "catalog_search",
                        "produces": "result",
                        "answer_role": "user_facing",
                    }
                ],
            }
        ],
    )
    validate_execution(plan, routed, DefaultToolContractProvider(), {})


class FixedModel:
    def __init__(self, name, args):
        self.name, self.args, self.messages = name, args, []

    async def ainvoke(self, messages):
        self.messages.append(messages)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.name,
                    "args": self.args,
                    "id": "call",
                }
            ],
        )


@pytest.mark.asyncio
async def test_mixed_goals_survive_conversion_and_only_tool_goal_reaches_llm2():
    message = "总结刚才的建议，再查订单123"
    router = FixedModel(
        "route_request",
        {
            "rewritten_query": message,
            "goals": [
                {
                    "id": "goal_1",
                    "query": "总结刚才的建议",
                    "source_text": "总结刚才的建议",
                    "disposition": "session_grounded_response",
                },
                {
                    "id": "goal_2",
                    "query": "查订单123",
                    "source_text": "查订单123",
                    "disposition": "tool_planning",
                },
            ],
        },
    )
    planner = FixedModel(
        "submit_execution_plan",
        {
            "tasks": [
                {
                    "goal_id": "goal_2",
                    "id": "task_1",
                    "tool_name": "order_lookup",
                    "arguments": {"order_id": 123, "query": "查订单123"},
                }
            ]
        },
    )
    runtime = SimpleNamespace(
        request_router=router, task_planner=planner, contract_provider=DefaultToolContractProvider()
    )
    state = {"message": message, "history": [{"role": "assistant", "content": "先前建议"}]}
    result = await route_goals(runtime, state)
    state["route_plan"] = result.model_dump()
    await plan_tools(runtime, state)
    payload = json.loads(planner.messages[0][-1].content)
    assert [g["id"] for g in payload["goals"]] == ["goal_2"]
    assert payload["goals"][0]["source_text"] == "查订单123"
    assert state["route_plan"]["subqueries"][0]["disposition"] == "session_grounded_response"
    assert state["execution_plan"]["tasks"][0]["arguments"] == {
        "order_id": 123,
        "query": "查订单123",
    }
    assert len(router.messages) == len(planner.messages) == 1


@pytest.mark.parametrize(
    "source,query,refs,valid",
    [
        ("查订单123", "查订单123", [], True),
        ("查订单123", "查订单456", [], False),
        ("第二笔的物流呢", "查订单456物流", ["order_2"], True),
        ("第二笔的物流呢", "查订单456物流", [], False),
    ],
)
def test_llm1_order_source(source, query, refs, valid):
    goal = Goal(
        id="goal_1", query=query, source_text=source, disposition="tool_planning", context_refs=refs
    )
    objects = {"order_2": {"kind": "order", "id": 456}}
    if valid:
        validate_goal_identity(goal, source, objects)
    else:
        with pytest.raises(ValueError, match="order IDs"):
            validate_goal_identity(goal, source, objects)


@pytest.mark.parametrize(
    "arguments,valid",
    [
        ({"order_id": 123, "query": "查订单123"}, True),
        ({"order_id": 123}, True),
        ({"order_id": 123, "query": None}, True),
        ({"query": "查订单123"}, True),
        ({}, False),
        ({"limit": 2}, False),
        ({"query": None}, False),
        ({"query": ""}, False),
        ({"query": " \t\n"}, False),
        ({"order_id": 123, "query": " \t\n"}, False),
        ({"order_id": 456}, False),
        ({"query": "查订单456"}, False),
        ({"query": "查202607020001物流"}, False),
        ({"order_id": 123, "query": "查订单456"}, False),
        ({"order_id": 0}, False),
        ({"order_id": -1}, False),
        ({"order_id": True}, False),
        ({"order_id": 123, "user_id": 999}, False),
    ],
)
def test_llm2_order_identity_and_runtime_field_validation(arguments, valid):
    plan = execution("order_lookup", arguments)

    def validate():
        validate_execution(plan, route("查订单123"), DefaultToolContractProvider(), {})

    if valid:
        validate()
    else:
        with pytest.raises(ValueError):
            validate()


def test_historical_order_uses_llm1_identity_without_runtime_rewrite():
    objects = {"order_2": {"kind": "order", "id": 456}}
    historical = route("查订单456物流", source_text="第二笔的物流呢", refs=["order_2"])
    binding = {"source": "context", "ref": "order_2", "kind": "order"}
    for bindings in ([], [binding]):
        plan = execution("order_lookup", {"query": "查订单456物流", "order_id": 456},
                         bindings=bindings)
        validate_execution(plan, historical, DefaultToolContractProvider(), objects)
        assert bind_arguments({}, plan.tasks[0], plan.tasks[0].arguments) == {
            "query": "查订单456物流", "order_id": 456,
        }
    for arguments, bindings in [
        ({"query": "查订单456物流"}, []),
        ({"query": "查订单456物流"}, [binding]),
        ({"order_id": 123}, [binding]),
    ]:
        with pytest.raises(ValueError):
            validate_execution(
                execution("order_lookup", arguments, bindings=bindings),
                historical, DefaultToolContractProvider(), objects,
            )
    with pytest.raises(ValueError, match="matching submitted order_id"):
        validate_execution(
            execution("order_lookup", {"order_id": 123}, bindings=[binding]),
            route("查订单123", refs=["order_2"]), DefaultToolContractProvider(), objects,
        )


@pytest.mark.parametrize(
    "tool,default,explicit,maximum",
    [
        ("catalog_search", 3, 2, 20),
        ("catalog_facets", 20, 4, 50),
        ("order_lookup", 5, 2, 5),
        ("knowledge_search", 3, 1, 10),
        ("policy_search", 3, 4, 10),
    ],
)
def test_omitted_limits_remain_omitted_and_explicit_limits_are_preserved(
    tool,
    default,
    explicit,
    maximum,
):
    provider = DefaultToolContractProvider()
    for value in (None, explicit):
        args = {"query": "查询"}
        if value is not None:
            args["limit"] = value
        plan = execution(tool, args)
        validate_execution(plan, route("查询"), provider, {})
        assert plan.tasks[0].arguments == args
        model = provider.get_contract(tool).public_input_model.model_validate(args)
        assert model.limit == (default if value is None else explicit)
    with pytest.raises(ValueError):
        validate_execution(
            execution(tool, {"query": "查询", "limit": maximum + 1}), route("查询"), provider, {}
        )


@pytest.mark.parametrize(
    "query,expected",
    [
        ("查订单123", 123),
        ("order ID: 42", 42),
        ("123", 123),
        ("查202607020001物流", 202607020001),
        ("查订单123和订单456", None),
        ("最近2笔订单", None),
        ("订单123的金额是否超过456元", 123),
    ],
)
def test_order_tool_and_planner_share_explicit_id_recognition(query, expected):
    assert _extract_order_id(query) == expected


@pytest.mark.parametrize("order_id", [None, 123])
@pytest.mark.parametrize("query_mode", ["present", "omitted", "null"])
def test_structured_order_call_does_not_reinterpret_old_memory(order_id, query_mode):
    def forbidden(*args):
        raise AssertionError("new plans must not infer an order from legacy memory")

    args = {"query": "这笔订单的物流"} if query_mode == "present" else {}
    if query_mode == "null":
        args["query"] = None
    if order_id is not None:
        args["order_id"] = order_id
    plan = execution("order_lookup", args)
    if order_id is None and query_mode != "present":
        with pytest.raises(ValidationError, match="query or an explicit order_id"):
            validate_execution(plan, route("查订单123"), DefaultToolContractProvider(), {})
        return
    validate_execution(plan, route("查订单123"), DefaultToolContractProvider(), {})
    args = plan.tasks[0].arguments
    runtime = SimpleNamespace(
        contract_provider=DefaultToolContractProvider(),
        memory_service=SimpleNamespace(resolve_order_id=forbidden),
    )
    state = {
        "message": "查订单123，另解释键盘轴体：" + "背景" * 145,
        "execution_plan": plan.model_dump(),
        "working_memory_snapshot": {"order": {"last_order_id": 999}},
    }
    call = PlannedToolCall(id="call_1", name="order_lookup", arguments=args, subquery="task_1")
    prepared, _ = AgentRuntime._prepare_tool_call(runtime, state, call)
    assert prepared.arguments.get("order_id") == order_id
    assert prepared.arguments == args


@pytest.mark.parametrize(
    "tool", ["catalog_search", "catalog_facets", "policy_search", "knowledge_search"],
)
@pytest.mark.parametrize(
    "arguments", [{}, {"query": None}, {"query": ""}, {"query": " \t\n\u3000"}],
)
def test_query_required_tools_reject_missing_or_blank_query(tool, arguments):
    provider = DefaultToolContractProvider()
    with pytest.raises(ValidationError):
        validate_execution(execution(tool, arguments), route("查询"), provider, {})
    schema = provider.get_contract(tool).public_input_model.model_json_schema()
    assert "query" in schema["required"]


@pytest.mark.parametrize("order_id", [None, 123])
@pytest.mark.parametrize("query", [None, "", " \n", "查询订单详情"])
def test_internal_order_input_matches_public_selection_requirement(order_id, query):
    args = {"order_id": order_id, "query": query}
    public = DefaultToolContractProvider().get_contract("order_lookup").public_input_model
    valid = query == "查询订单详情" or (order_id == 123 and query is None)
    for model, extra in ((public, {}), (OrderLookupInput, {"user_id": 7})):
        if valid:
            assert model.model_validate({**args, **extra}).order_id == order_id
        else:
            with pytest.raises(ValidationError):
                model.model_validate({**args, **extra})


@pytest.mark.asyncio
async def test_order_owner_predicate_and_server_identity_without_configured_database(monkeypatch):
    # Execute the real repository WHERE expression against a disposable SQLite table.
    # No ORM models, fixtures or connections from the configured database are used.
    db = sqlite3.connect(":memory:")
    table = OrderInfo.__tablename__
    db.execute(f"CREATE TABLE {table} (id INTEGER, user_id INTEGER)")
    db.executemany(f"INSERT INTO {table} VALUES (?, ?)", [(123, 7), (456, 8)])
    seen = []

    class Session:
        async def execute(self, statement):
            predicate = statement.whereclause.compile(compile_kwargs={"literal_binds": True})
            row = db.execute(f"SELECT id FROM {table} WHERE {predicate}").fetchone()
            return SimpleNamespace(scalar_one_or_none=lambda: row[0] if row else None)

    monkeypatch.setattr(order_repository, "_to_order_card", lambda identity: identity)
    repository = order_repository.OrderRepository(Session())

    async def handler(request):
        seen.append((request.user_id, request.order_id))
        assert await repository.get_order(request.user_id, request.order_id) is None
        return OrderLookupOutput(result_type="not_found")

    try:
        assert await repository.get_order(7, 123) == 123
        contract = DefaultToolContractProvider().get_contract("order_lookup")
        result = await execute_bound_tool(
            BoundTool(contract, handler), {"order_id": 456}, {"user_id": 7}
        )
        assert result.ok and result.output["result_type"] == "not_found"
        assert seen == [(7, 456)]
        forged = await execute_bound_tool(
            BoundTool(contract, handler), {"order_id": 456, "user_id": 8}, {"user_id": 7}
        )
        assert not forged.ok and forged.error.code == "invalid_input"
        assert seen == [(7, 456)]
    finally:
        db.close()
