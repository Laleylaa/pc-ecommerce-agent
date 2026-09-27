from typing import cast

import pytest

from app.agent.artifacts import initialize_task_runtime
from app.agent.graph import AgentRuntime, _suggest_actions
from app.agent.state import AgentState
from app.core.config import Settings


def _tool_route_plan(query: str) -> dict:
    return {
        "rewritten_query": query,
        "subqueries": [
            {
                "id": "sq_1",
                "query": query,
                "disposition": "tool_planning",
                "reason_code": "test_tool_planning",
            }
        ],
    }


@pytest.mark.asyncio
async def test_product_route_only_uses_current_message() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))

    state = cast(
        AgentState,
        {
            "message": "推荐 500 元以内的无线鼠标",
            "route_plan": _tool_route_plan("推荐 500 元以内的无线鼠标"),
            "tool_results": [],
        },
    )
    initialize_task_runtime(state)
    decision = runtime._fallback_planner_decision(state)

    tool_input = decision.tool_calls[0].arguments
    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name == "catalog_search"
    assert tool_input == {
        "query": "推荐 500 元以内的无线鼠标",
        "limit": 3,
    }


@pytest.mark.asyncio
async def test_order_route_only_uses_explicit_order_id() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))

    state = cast(
        AgentState,
        {
            "message": "查询订单 202607020001 的物流",
            "route_plan": _tool_route_plan("查询订单 202607020001 的物流"),
            "tool_results": [],
        },
    )
    initialize_task_runtime(state)
    decision = runtime._fallback_planner_decision(state)

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name == "order_lookup"
    assert decision.tool_calls[0].arguments["order_id"] == 202607020001


def test_identity_question_is_resolved_by_router_without_tool_call() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "message": "你是谁，你能做什么？",
            "working_memory": {},
        },
    )

    plan = runtime._fallback_route_plan(state)

    assert plan.subqueries[0].disposition == "direct_response"


def test_handoff_uses_order_id_from_current_message() -> None:
    state = cast(
        AgentState,
        {
            "message": "订单 202607020001 我要退货",
            "boundary": {
                "classification": "human_handoff_required",
                "reason": "涉及售后、订单变更或其他需要人工确认的写操作",
                "display_message": "这个请求需要人工客服确认后处理。",
            },
            "intent": "after_sales",
        },
    )

    actions = _suggest_actions(state)

    assert actions == [
        {
            "label": "转人工客服",
            "payload": {
                "handoff": True,
                "orderId": 202607020001,
                "requestType": "return",
                "reason": "订单 202607020001 我要退货",
            },
        }
    ]
