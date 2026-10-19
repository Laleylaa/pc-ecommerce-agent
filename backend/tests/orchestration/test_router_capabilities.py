from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.agent.capabilities import decision_from_route_capabilities
from app.agent.graph import AgentRuntime
from app.agent.routing import RequestRoutePlan
from app.agent.state import AgentState
from app.core.config import Settings


def _plan(query: str, capability: str) -> RequestRoutePlan:
    return RequestRoutePlan.model_validate(
        {
            "rewritten_query": query,
            "subqueries": [
                {
                    "id": "sq_1",
                    "query": query,
                    "disposition": "tool_planning",
                    "reason_code": "explicit_single_capability",
                    "capability": capability,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("query", "capability", "expected_name"),
    [
        ("推荐 500 元以内的无线鼠标", "catalog_search", "catalog_search"),
        ("有哪些鼠标品牌", "catalog_facets", "catalog_facets"),
        ("介绍机械键盘轴体", "knowledge_search", "knowledge_search"),
    ],
)
def test_concrete_router_capability_builds_direct_wave(
    query: str,
    capability: str,
    expected_name: str,
) -> None:
    decision = decision_from_route_capabilities(
        _plan(query, capability)
    )

    assert decision is not None
    assert decision.reason == "router_capability_direct_wave"
    assert decision.tool_calls[0].name == expected_name
    assert decision.tool_calls[0].subquery == "sq_1"


@pytest.mark.parametrize(
    ("query", "capability"),
    [
        ("推荐无线鼠标", "planner_required"),
        ("比较这两款鼠标", "planner_required"),
    ],
)
def test_ambiguous_or_structurally_incomplete_capability_falls_back_to_planner(
    query: str, capability: str
) -> None:
    assert decision_from_route_capabilities(_plan(query, capability)) is None


def test_runtime_does_not_reclassify_concrete_router_capability_from_task_text() -> None:
    decision = decision_from_route_capabilities(
        _plan("把这两款鼠标放在一起看看", "catalog_search")
    )

    assert decision is not None
    assert decision.tool_calls[0].name == "catalog_search"


def test_non_tool_route_cannot_smuggle_capability() -> None:
    with pytest.raises(ValidationError):
        RequestRoutePlan.model_validate(
            {
                "rewritten_query": "帮我取消订单",
                "subqueries": [
                    {
                        "id": "sq_1",
                        "query": "帮我取消订单",
                        "disposition": "human_handoff",
                        "reason_code": "write_not_supported",
                        "capability": "order_lookup",
                    }
                ],
            }
        )


def test_route_plan_rejects_removed_unsupported_disposition() -> None:
    with pytest.raises(ValidationError):
        RequestRoutePlan.model_validate(
            {
                "rewritten_query": "帮我取消订单",
                "subqueries": [
                    {
                        "id": "sq_1",
                        "query": "帮我取消订单",
                        "disposition": "unsupported",
                        "reason_code": "legacy_capability_gap",
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_runtime_skips_first_planner_call_for_accepted_capability() -> None:
    class NeverInvokeModel:
        def __init__(self) -> None:
            self.call_count = 0

        def bind_tools(
            self, tools: list[dict[str, Any]], **_: Any
        ) -> "NeverInvokeModel":
            return self

        async def ainvoke(self, messages: list[Any]) -> Any:
            self.call_count += 1
            raise AssertionError("accepted Router capability must skip the first Planner LLM")

    model = NeverInvokeModel()
    runtime = AgentRuntime(
        cast(Any, None),
        Settings(llm_api_key=""),
        chat_model=model,
    )
    state = cast(
        AgentState,
        {
            "message": "推荐 500 元以内的无线鼠标",
            "route_plan": _plan(
                "推荐 500 元以内的无线鼠标", "catalog_search"
            ).model_dump(mode="json"),
            "tool_results": [],
            "tool_waves": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )

    result = await runtime._orchestrate(state)

    assert model.call_count == 0
    assert result["orchestrator_call_count"] == 0
    assert result["decision"]["tool_calls"][0]["arguments"] == {
        "query": "推荐 500 元以内的无线鼠标",
        "limit": 3,
    }
