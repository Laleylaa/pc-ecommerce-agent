from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.artifacts import initialize_task_runtime
from app.agent.decisions import PlannedToolCall
from app.agent.graph import AgentRuntime, _fallback_catalog_query
from app.agent.route_runtime import _contextual_intent
from app.agent.state import AgentState
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.schemas.context import (
    MemoryChanges,
    PreparedTurn,
    StructuredMemory,
    WorkingMemoryV2,
)
from app.services.memory import MemoryService
from app.tools.contracts import ToolContract
from app.tools.schemas import ToolError, ToolExecutionResult

PRODUCT = {
    "spu_id": 10,
    "sku_id": 101,
    "title": "Razer Test Mouse",
    "brand": "Razer",
    "category": "mouse",
    "price": "399.00",
    "stock": 8,
    "sku_sales_count": 5,
    "sku_sales_count_scope": "sku",
    "sales_count": 12,
    "sales_count_scope": "spu",
    "specs": {"connection_type": "Wireless"},
    "image_url": None,
}
SECOND_PRODUCT = {
    **PRODUCT,
    "spu_id": 11,
    "sku_id": 102,
    "title": "Logitech Fresh Mouse",
    "brand": "Logitech",
    "price": "299.00",
}
ORDER = {
    "id": 202607020001,
    "status": 3,
    "status_label": "已发货",
    "pay_amount": "399.00",
    "created_at": "2026-07-02T10:00:00",
    "items": [
        {
            "id": 1,
            "sku_id": 101,
            "sku_name": "Razer Test Mouse",
            "sku_specs": {"connection_type": "Wireless"},
            "price": "399.00",
            "quantity": 1,
        }
    ],
    "logistics": {
        "express_company": "顺丰",
        "logistic_no": "SF123",
        "status": 2,
        "trace": [],
    },
}


class FakeContextService:
    def __init__(self, prepared: PreparedTurn):
        self.prepared = prepared
        self.prepare_calls: list[tuple[int, int | None, str]] = []
        self.completed_outcomes: list[dict[str, Any]] = []

    async def prepare_turn(
        self, user_id: int, conversation_id: int | None, message: str
    ) -> PreparedTurn:
        self.prepare_calls.append((user_id, conversation_id, message))
        return self.prepared

    async def complete_turn(
        self, prepared_turn: PreparedTurn, outcome: dict[str, Any]
    ) -> MemoryChanges:
        assert prepared_turn is self.prepared
        self.completed_outcomes.append(outcome)
        return MemoryChanges(
            working_memory=prepared_turn.working_memory,
            audit={"retained_turns": prepared_turn.retained_turns},
        )


class FakeToolExecutor:
    def __init__(self, results: dict[str, ToolExecutionResult]):
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.runtime_contexts: list[dict[str, Any]] = []

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult:
        name = contract.registry_name
        self.calls.append((name, dict(arguments)))
        self.runtime_contexts.append(dict(runtime_context))
        return self.results[name]


def test_catalog_tool_call_preserves_query_only_public_input() -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "message": "推荐 500 元以内无线鼠标",
            "working_memory": WorkingMemoryV2().model_dump(mode="json"),
            "memory": [],
        },
    )
    call = PlannedToolCall(
        id="catalog-call",
        name="catalog_search",
        arguments={
            "query": "推荐 500 元以内无线鼠标",
            "limit": 5,
        },
    )

    prepared_call, _ = runtime._prepare_tool_call(state, call)

    assert prepared_call.arguments == {
        "query": "推荐 500 元以内无线鼠标",
        "limit": 5,
    }


def test_order_list_call_ignores_planner_limit_one() -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "message": "列出我最近的订单",
            "working_memory_snapshot": WorkingMemoryV2().model_dump(mode="json"),
        },
    )
    call = PlannedToolCall(
        id="order-list",
        name="order_lookup",
        arguments={"limit": 1},
    )

    prepared_call, _ = runtime._prepare_tool_call(state, call)

    assert prepared_call.arguments == {
        "query": "列出我最近的订单",
        "limit": 5,
        "offset": 0,
    }


@pytest.mark.parametrize(
    "followup",
    [
        "继续看下一页",
        "那剩下的也给我看看",
        "还有别的订单吗",
    ],
)
def test_order_followups_bind_ordinal_and_remaining_from_memory(followup: str) -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    memory = WorkingMemoryV2.model_validate(
        {
            "order": {
                "candidate_order_ids": [202607020001, 202607020002],
                "total_match_count": 7,
                "returned_count": 2,
                "is_exhaustive": False,
                "next_offset": 2,
            }
        }
    ).model_dump(mode="json")

    ordinal_call, _ = runtime._prepare_tool_call(
        cast(
            AgentState,
            {"message": "就看第二个订单", "working_memory_snapshot": memory},
        ),
        PlannedToolCall(id="order-detail", name="order_lookup", arguments={}),
    )
    page_call, _ = runtime._prepare_tool_call(
        cast(AgentState, {"message": followup, "working_memory_snapshot": memory}),
        PlannedToolCall(id="order-page", name="order_lookup", arguments={}),
    )

    assert ordinal_call.arguments["order_id"] == 202607020002
    assert page_call.arguments == {
        "query": followup,
        "limit": 5,
        "offset": 2,
    }


def test_colloquial_remaining_orders_followup_keeps_order_intent() -> None:
    memory = {
        "order": {
            "candidate_order_ids": [202607020001],
            "next_offset": 1,
        }
    }

    intent = _contextual_intent(
        "那剩下的也都给我吧",
        memory,
        MemoryService(),
    )

    assert intent == "order_status"


def test_estimated_arrival_followup_keeps_order_intent() -> None:
    memory = {
        "order": {
            "last_order_id": 991000000103,
            "candidate_order_ids": [991000000103],
        }
    }

    intent = _contextual_intent(
        "这个多久能到？",
        memory,
        MemoryService(),
    )

    assert intent == "order_status"

    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    prepared_call, _ = runtime._prepare_tool_call(
        cast(
            AgentState,
            {
                "message": "这个多久能到？",
                "working_memory_snapshot": memory,
            },
        ),
        PlannedToolCall(id="eta", name="order_lookup", arguments={}),
    )

    assert prepared_call.arguments["order_id"] == 991000000103
    assert prepared_call.arguments["query"] == "这个多久能到？"


def test_single_order_goal_preserves_original_field_requirements() -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "message": "我到底有多少订单呀？",
            "route_plan": {
                "rewritten_query": "查询用户订单",
                "subqueries": [
                    {
                        "id": "goal_1",
                        "query": "查询所有订单",
                        "disposition": "tool_planning",
                        "reason_code": "order_lookup",
                        "tasks": [
                            {
                                "id": "task_1",
                                "goal_id": "goal_1",
                                "canonical_query": "查询所有订单",
                                "depends_on": [],
                                "input_requirements": [],
                                "produces": "order",
                                "answer_role": "user_facing",
                                "capability": "order_lookup",
                            }
                        ],
                    }
                ],
            },
            "working_memory_snapshot": WorkingMemoryV2().model_dump(mode="json"),
        },
    )

    prepared_call, _ = runtime._prepare_tool_call(
        state,
        PlannedToolCall(
            id="count-orders",
            name="order_lookup",
            arguments={},
            subquery="task_1",
        ),
    )

    assert prepared_call.arguments["query"] == "我到底有多少订单呀？"


@pytest.mark.parametrize("tool_name", ["policy_search", "knowledge_search"])
def test_document_search_runtime_supplies_default_top_k(tool_name: str) -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "message": "我想了解一下退货",
            "working_memory": WorkingMemoryV2().model_dump(mode="json"),
            "memory": [],
        },
    )
    call = PlannedToolCall(
        id="document-call",
        name=tool_name,
        arguments={"limit": 1},
    )

    prepared_call, _ = runtime._prepare_tool_call(state, call)

    assert prepared_call.arguments == {
        "query": "我想了解一下退货",
        "limit": 3,
    }


def test_catalog_tool_call_rejects_internal_planner_fields() -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "message": "推荐有线鼠标",
            "working_memory": WorkingMemoryV2().model_dump(mode="json"),
            "memory": [],
        },
    )
    call = PlannedToolCall(
        id="catalog-call",
        name="catalog_search",
        arguments={
            "query": "有线鼠标",
            "filters": {"connection_type": "Wired"},
        },
    )

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        runtime._prepare_tool_call(state, call)


@pytest.mark.asyncio
async def test_invalid_catalog_tool_arguments_become_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_calls: list[tuple[Any, ...]] = []

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            captured_calls.append(args)

    registry = FakeToolExecutor({})
    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        tool_executor=registry,
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("execute_tool_wave", runtime._execute_tool_wave)
    workflow.set_entry_point("execute_tool_wave")
    workflow.add_edge("execute_tool_wave", END)

    result = await workflow.compile().ainvoke(
        cast(
            AgentState,
            {
                "user_id": 7,
                "run_id": 61,
                "message": "推荐鼠标",
                "decision": {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "catalog-call",
                            "name": "catalog_search",
                            "arguments": {
                                "query": "鼠标",
                                "unexpected_filter": "value",
                            },
                        }
                    ],
                },
                "working_memory": WorkingMemoryV2().model_dump(mode="json"),
                "memory": [],
                "parsed": {},
                "products": [],
                "evidence": [],
                "order": None,
                "tool_waves": [],
                "tool_results": [],
                "tool_wave_count": 0,
            },
        )
    )

    execution = result["tool_results"][0]["execution"]
    assert execution["ok"] is False
    assert execution["error"]["code"] == "invalid_input"
    assert execution["error"]["retryable"] is True
    assert execution["error"]["recommended_action"] == "replan_arguments"
    assert registry.calls == []
    assert captured_calls[0][1] == "catalog_search"


@pytest.mark.asyncio
async def test_equivalent_tool_call_reuses_previous_usable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_calls: list[tuple[Any, ...]] = []

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            captured_calls.append(args)

    registry = FakeToolExecutor({})
    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        tool_executor=registry,
    )
    previous_execution = {
        "tool_name": "catalog.facets",
        "ok": True,
        "output": {
            "result_type": "facets",
            "facet": "brand",
            "items": [{"value": "Logitech", "count": 12}],
            "query_plan": {},
        },
        "error": None,
    }
    state = cast(
        AgentState,
        {
            "user_id": 7,
            "run_id": 61,
            "message": "有哪些鼠标品牌",
            "decision": {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": "facets-2",
                        "name": "catalog_facets",
                        "arguments": {
                            "query": "  MOUSE   BRANDS ",
                        },
                    }
                ],
            },
            "parsed": {},
            "products": [],
            "evidence": [],
            "order": None,
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "facets-1",
                            "name": "catalog_facets",
                            "arguments": {
                                "query": "mouse brands",
                                "limit": 20,
                            },
                        }
                    ],
                    "results": [
                        {
                            "tool_call_id": "facets-1",
                            "name": "catalog_facets",
                            "execution": previous_execution,
                        }
                    ],
                }
            ],
            "tool_results": [
                {
                    "tool_call_id": "facets-1",
                    "name": "catalog_facets",
                    "execution": previous_execution,
                }
            ],
            "tool_wave_count": 1,
        },
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("execute_tool_wave", runtime._execute_tool_wave)
    workflow.add_node("normalize_tool_results", runtime._normalize_tool_results)
    workflow.add_node("update_subquery_ledger", runtime._update_subquery_ledger)
    workflow.set_entry_point("execute_tool_wave")
    workflow.add_edge("execute_tool_wave", "normalize_tool_results")
    workflow.add_edge("normalize_tool_results", "update_subquery_ledger")
    workflow.add_edge("update_subquery_ledger", END)

    result = await workflow.compile().ainvoke(state)

    assert registry.calls == []
    assert captured_calls == []
    assert result["tool_wave_count"] == 2
    reused = result["tool_waves"][1]["results"][0]
    assert reused["reused_from_tool_call_id"] == "facets-1"
    assert reused["execution"] == previous_execution
    assert result["subquery_ledger"][1]["outcome"] == "usable"
    assert result["subquery_ledger"][1]["reused_from_tool_call_id"] == "facets-1"


@pytest.mark.asyncio
async def test_sync_runtime_uses_context_and_catalog_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=51,
        run_id=61,
        message="推荐 500 元以内无线鼠标",
        retained_turns=2,
    )
    context = FakeContextService(prepared)
    registry = FakeToolExecutor(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "products",
                    "products": [PRODUCT],
                    "ranking_strategy": "test",
                    "query_plan": {
                        "query": "无线鼠标",
                        "category": "mouse",
                        "max_price": "500",
                        "filters": {"connection_type": "Wireless"},
                    },
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=registry,
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert context.prepare_calls == [(7, None, prepared.message)]
    assert [name for name, _ in registry.calls] == ["catalog.search"]
    assert response.products[0].sku_id == 101
    assert response.products[0].sku_sales_count == 5
    assert response.products[0].sales_count == 12
    assert response.products[0].sku_sales_count_scope == "sku"
    assert response.products[0].sales_count_scope == "spu"
    assert context.completed_outcomes
    assert "working_memory" not in context.completed_outcomes[0]


@pytest.mark.asyncio
async def test_compare_followup_resolves_ordinals_to_working_memory_sku_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=52,
        run_id=62,
        message="对比第一个和第二个",
        working_memory=WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {"query": "mouse", "category": "mouse"},
                    "candidate_spu_ids": [10, 11],
                    "candidate_sku_ids": [101, 102],
                }
            }
        ),
    )
    context = FakeContextService(prepared)
    registry = FakeToolExecutor(
        {
            "catalog.compare": ToolExecutionResult(
                tool_name="catalog.compare",
                ok=True,
                output={
                    "result_type": "comparison",
                    "products": [PRODUCT, SECOND_PRODUCT],
                    "comparison_fields": ["price", "connection_type"],
                    "missing_fields": {},
                    "query_plan": {
                        "mode": "direct_sku_ids",
                        "sku_ids": [101, 102],
                        "query": prepared.message,
                        "compare_plan": None,
                    },
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=registry,
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert registry.calls == [
        (
            "catalog.compare",
            {
                "query": "此前商品需求：mouse；当前补充要求：对比第一个和第二个",
                "limit": 5,
            },
        )
    ]
    assert registry.runtime_contexts[0]["user_id"] == 7
    catalog_context = registry.runtime_contexts[0]["catalog_context"]
    assert catalog_context["memory"]["candidate_sku_ids"] == [101, 102]
    assert catalog_context["memory"]["candidate_spu_ids"] == [10, 11]
    assert catalog_context["dependency_artifacts"] == []
    assert [product.sku_id for product in response.products] == [101, 102]
    assert response.products[1].title == "Logitech Fresh Mouse"


@pytest.mark.asyncio
async def test_catalog_fallback_sends_current_query_without_structured_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=53,
        run_id=63,
        message="这次要 800 元以内 Logitech 无线办公鼠标",
        working_memory=WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {
                        "query": "gaming mouse",
                        "category": "mouse",
                        "brands": ["Razer"],
                        "max_price": 500,
                        "filters": {"connection_type": "Wired"},
                        "usage_scenario": "gaming",
                    }
                }
            }
        ),
        memory=[
            StructuredMemory(
                id=71,
                scope="user",
                fact_type="preference",
                key="brand_preference",
                value="偏好 SteelSeries 品牌",
                value_json={"brand": "SteelSeries", "negated": False},
                confidence=0.8,
            ),
            StructuredMemory(
                id=72,
                scope="user",
                fact_type="preference",
                key="budget_preference",
                value="偏好 300 元以内预算",
                value_json={"amount": 300, "maximum": True},
                confidence=0.8,
            ),
        ],
    )
    context = FakeContextService(prepared)
    registry = FakeToolExecutor(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=registry,
    )

    await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    _, tool_input = registry.calls[0]
    assert tool_input == {
        "query": prepared.message,
        "limit": 3,
    }
    assert context.completed_outcomes[0].get("applied_memory_ids", []) == []


@pytest.mark.asyncio
async def test_sync_runtime_uses_authenticated_order_lookup_and_preserves_order_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=54,
        run_id=64,
        message="帮我查最近订单",
    )
    context = FakeContextService(prepared)

    class SequenceToolExecutor:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def execute(
            self,
            contract: ToolContract,
            arguments: dict[str, Any],
            runtime_context: dict[str, Any],
        ) -> ToolExecutionResult:
            name = contract.registry_name
            input_data = dict(arguments)
            for field_name in contract.runtime_fields:
                input_data[field_name] = runtime_context[field_name]
            self.calls.append((name, input_data))
            if len(self.calls) == 1:
                return ToolExecutionResult(
                    tool_name=name,
                    ok=True,
                    output={
                        "result_type": "order_candidates",
                        "order": None,
                        "candidates": [
                            {
                                "id": ORDER["id"],
                                "status": ORDER["status"],
                                "status_label": ORDER["status_label"],
                                "pay_amount": ORDER["pay_amount"],
                                "created_at": ORDER["created_at"],
                                "item_count": 1,
                                "first_item_name": "Razer Test Mouse",
                                "logistic_no": "SF123",
                            }
                        ],
                    },
                )
            return ToolExecutionResult(
                tool_name=name,
                ok=True,
                output={"result_type": "single_order", "order": ORDER, "candidates": []},
            )

    executor = SequenceToolExecutor()

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)

    class OrderDagRouter:
        def bind_tools(self, _: list[dict[str, Any]], **__: Any) -> "OrderDagRouter":
            return self

        async def ainvoke(self, _: list[Any]) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "route-order-dag",
                        "name": "route_request",
                        "args": {
                            "rewritten_query": prepared.message,
                            "subqueries": [
                                {
                                    "id": "goal_1",
                                    "query": prepared.message,
                                    "disposition": "tool_planning",
                                    "reason_code": "latest_order_detail",
                                    "tasks": [
                                        {
                                            "id": "task_1",
                                            "goal_id": "goal_1",
                                            "canonical_query": "查询最近订单候选",
                                            "depends_on": [],
                                            "input_requirements": [],
                                            "produces": "order",
                                            "answer_role": "internal",
                                            "capability": "order_lookup",
                                        },
                                        {
                                            "id": "task_2",
                                            "goal_id": "goal_1",
                                            "canonical_query": "查询选中订单详情",
                                            "depends_on": ["task_1"],
                                            "input_requirements": [
                                                {
                                                    "name": "order_id",
                                                    "source": "task_output",
                                                    "task_id": "task_1",
                                                }
                                            ],
                                            "produces": "order",
                                            "answer_role": "user_facing",
                                            "capability": "order_lookup",
                                        },
                                    ],
                                }
                            ],
                        },
                        "type": "tool_call",
                    }
                ],
            )

    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        router_model=OrderDagRouter(),
        context_service=context,
        tool_executor=executor,
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert executor.calls == [
        (
            "order.lookup",
            {"query": "查询最近订单候选", "limit": 5, "offset": 0, "user_id": 7},
        ),
        (
            "order.lookup",
            {
                "order_id": ORDER["id"],
                "query": "查询选中订单详情",
                "limit": 5,
                "offset": 0,
                "user_id": 7,
            },
        ),
    ]
    assert response.order is not None
    assert response.order.id == ORDER["id"]
    assert response.order.logistics is not None
    assert response.order.logistics.logistic_no == "SF123"


@pytest.mark.asyncio
async def test_stream_registry_failure_emits_tool_error_and_finishes_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=55,
        run_id=65,
        message="推荐无线鼠标",
        working_memory=WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {"query": "old mouse", "category": "mouse"},
                    "candidate_spu_ids": [99],
                    "candidate_sku_ids": [999],
                }
            }
        ),
    )
    context = FakeContextService(prepared)
    registry = FakeToolExecutor(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=False,
                error=ToolError(code="catalog_unavailable", message="temporary outage"),
            )
        }
    )

    class FakeSession:
        async def commit(self) -> None:
            pass

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

        async def fail_run(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, FakeSession()),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=registry,
    )

    events = [
        event
        async for event in runtime.run_stream(
            ChatRequest(message=prepared.message), user_id=7
        )
    ]

    catalog_events = [
        event
        for event in events
        if event["type"] == "tool_call" and event.get("tool_name") == "catalog_search"
    ]
    assert [event["status"] for event in catalog_events] == ["started", "error"]
    assert catalog_events[-1]["output"]["error"]["code"] == "catalog_unavailable"
    assert not any(event["type"] == "error" for event in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["response"]["products"] == []
    assert context.completed_outcomes
    assert "product_search" not in context.completed_outcomes[0]["parsed"]
    assert "working_memory" not in context.completed_outcomes[0]


@pytest.mark.asyncio
async def test_v2_working_query_plan_routes_generic_catalog_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=56,
        run_id=66,
        message="换成无线",
        working_memory=WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {
                        "query": "mouse",
                        "category": "mouse",
                        "max_price": 500,
                        "filters": {"connection_type": "Wired"},
                    },
                    "candidate_sku_ids": [101],
                }
            }
        ),
    )
    context = FakeContextService(prepared)
    registry = FakeToolExecutor(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=registry,
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert response.intent == "catalog_search"
    assert registry.calls[0][0] == "catalog.search"
    tool_input = registry.calls[0][1]
    assert tool_input == {
        "query": "此前商品需求：mouse；当前补充要求：换成无线",
        "limit": 3,
    }


def test_fallback_catalog_followup_keeps_memory_as_natural_language_context() -> None:
    working_memory = WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {
                    "query": "fps ergonomic mouse",
                    "category": "mouse",
                    "brands": ["Razer"],
                    "excluded_brands": ["Logitech"],
                    "excluded_usage": ["office"],
                    "min_price": 200,
                    "max_price": 500,
                    "filters": {
                        "connection_type": "Wired",
                        "max_dpi": "20000",
                        "hand_orientation": "Right",
                        "tracking_method": "Optical",
                    },
                    "keywords": ["fps", "lightweight"],
                    "usage_scenario": "gaming",
                    "sort": "price_asc",
                    "limit": 6,
                },
                "comparison": {
                    "query": "对比第一个和第二个",
                    "sku_ids": [101, 102],
                    "comparison_fields": ["price", "max_dpi"],
                },
                "candidate_sku_ids": [101, 102],
            }
        }
    )
    state = cast(
        Any,
        {
            "message": "换成无线",
            "working_memory": working_memory.model_dump(mode="json"),
            "memory": [],
        },
    )

    query = _fallback_catalog_query(state)

    assert query == "此前商品需求：fps ergonomic mouse；当前补充要求：换成无线"
    assert "max_dpi" not in query
    assert "price_asc" not in query


def test_bare_v2_brand_exclusion_routes_back_to_query_first_catalog_search() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    working_memory = WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {
                    "query": "gaming mouse",
                    "category": "mouse",
                    "brands": ["Razer"],
                }
            }
        }
    )

    state = cast(
        AgentState,
        {
            "message": "不要 Razer",
            "working_memory": working_memory.model_dump(mode="json"),
            "working_memory_snapshot": working_memory.model_dump(mode="json"),
            "memory": [],
            "tool_results": [],
        },
    )
    state["route_plan"] = runtime._fallback_route_plan(state).model_dump(mode="json")
    initialize_task_runtime(state)
    decision = runtime._fallback_planner_decision(state)

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name == "catalog_search"
    assert decision.tool_calls[0].arguments == {
        "query": "此前商品需求：gaming mouse；当前补充要求：不要 Razer",
        "limit": 3,
    }


@pytest.mark.asyncio
async def test_failed_stream_run_uses_compact_context_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=57,
        run_id=67,
        message="推荐鼠标",
        estimated_token_count=42,
        retained_turns=2,
        dropped_turns=3,
    )
    captured: dict[str, Any] = {}

    class FakeSession:
        async def commit(self) -> None:
            captured["committed"] = True

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def fail_run(
            self,
            run_id: int,
            intent: str | None,
            state: dict[str, Any],
            error: dict[str, str],
        ) -> None:
            captured.update(
                {"run_id": run_id, "intent": intent, "state": state, "error": error}
            )

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, FakeSession()),
        Settings(llm_api_key=""),
        context_service=FakeContextService(prepared),
        tool_executor=FakeToolExecutor({}),
    )
    state = cast(
        Any,
        {
            "run_id": 67,
            "intent": "product_recommendation",
            "prepared_turn": prepared,
            "history": [{"role": "user", "content": "secret history"}],
            "memory": [{"id": 1, "value": "secret memory"}],
            "working_memory": {"catalog": {"candidate_sku_ids": [101]}},
            "applied_memory_ids": [1],
        },
    )

    await runtime._mark_stream_failed(state, "RuntimeError", "boom")

    audit = captured["state"]
    assert "prepared_turn" not in audit
    assert "history" not in audit
    assert "memory" not in audit
    assert "working_memory" not in audit
    assert audit["estimated_token_count"] == 42
    assert audit["retained_turns"] == 2
    assert audit["dropped_turns"] == 3
    assert audit["applied_memory_ids"] == [1]
    assert captured["committed"] is True


@pytest.mark.asyncio
async def test_sync_runtime_tool_exception_degrades_to_safe_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=58,
        run_id=68,
        message="推荐鼠标",
        estimated_token_count=21,
        retained_turns=1,
        dropped_turns=4,
    )
    captured: dict[str, Any] = {}

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            captured["tool_call"] = args

        async def fail_run(self, run_id, intent, state, error) -> None:
            captured["failed_run"] = (run_id, intent, state, error)

    class RaisingToolExecutor:
        async def execute(
            self,
            contract: ToolContract,
            arguments: dict[str, Any],
            runtime_context: dict[str, Any],
        ) -> ToolExecutionResult:
            raise RuntimeError("executor boom")

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    context = FakeContextService(prepared)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=RaisingToolExecutor(),
    )

    response = await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert response.intent == "catalog_search"
    assert response.products == []
    assert "查询暂时失败" in response.answer
    assert captured["tool_call"][3]["error"]["code"] == "RuntimeError"
    assert "failed_run" not in captured
    assert context.completed_outcomes[0]["catalog_tool_succeeded"] is False
    assert "product_search" not in context.completed_outcomes[0]["parsed"]


@pytest.mark.asyncio
async def test_current_negative_catalog_request_stays_in_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=59,
        run_id=69,
        message="不要 Logitech 的游戏无线鼠标",
    )
    context = FakeContextService(prepared)
    registry = FakeToolExecutor(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=registry,
    )

    await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    tool_input = registry.calls[0][1]
    assert tool_input == {"query": prepared.message, "limit": 3}


@pytest.mark.asyncio
async def test_fallback_does_not_emit_long_term_preferences_as_tool_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=60,
        run_id=70,
        message="推荐鼠标",
        memory=[
            StructuredMemory(
                id=81,
                scope="user",
                fact_type="preference",
                key="brand_preference",
                value="不偏好 Logitech 品牌",
                value_json={"brand": "Logitech", "negated": True},
                confidence=0.8,
            ),
            StructuredMemory(
                id=82,
                scope="user",
                fact_type="preference",
                key="usage_preference",
                value="不偏好游戏场景",
                value_json={"usage": "gaming", "negated": True},
                confidence=0.8,
            ),
        ],
    )
    context = FakeContextService(prepared)
    registry = FakeToolExecutor(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=registry,
    )

    await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert registry.calls[0][1] == {"query": prepared.message, "limit": 3}
    assert context.completed_outcomes[0].get("applied_memory_ids", []) == []


@pytest.mark.asyncio
async def test_fallback_does_not_emit_working_memory_as_tool_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = PreparedTurn(
        user_id=7,
        conversation_id=41,
        user_message_id=61,
        run_id=71,
        message="推荐鼠标",
        working_memory=WorkingMemoryV2.model_validate(
            {"catalog": {"query_plan": {"excluded_brands": ["Logitech"]}}}
        ),
        memory=[
            StructuredMemory(
                id=83,
                scope="user",
                fact_type="preference",
                key="brand_preference",
                value="偏好 Logitech 品牌",
                value_json={"brand": "Logitech", "negated": False},
                confidence=0.8,
            )
        ],
    )
    context = FakeContextService(prepared)
    registry = FakeToolExecutor(
        {
            "catalog.search": ToolExecutionResult(
                tool_name="catalog.search",
                ok=True,
                output={
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "test",
                    "query_plan": {},
                },
            )
        }
    )

    class FakeAuditRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeAuditRepository)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=context,
        tool_executor=registry,
    )

    await runtime.run(ChatRequest(message=prepared.message), user_id=7)

    assert registry.calls[0][1] == {"query": prepared.message, "limit": 3}
    assert context.completed_outcomes[0].get("applied_memory_ids", []) == []
