from typing import Any, cast

import pytest
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.decisions import PlannedToolCall
from app.agent.graph import AgentRuntime
from app.agent.route_runtime import _contextual_intent
from app.agent.state import AgentState
from app.core.config import Settings
from app.schemas.context import (
    MemoryChanges,
    PreparedTurn,
    WorkingMemoryV2,
)
from app.services.memory import MemoryService
from app.tools.contracts import ToolContract
from app.tools.schemas import ToolExecutionResult


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


def test_order_list_call_preserves_planner_limit_one() -> None:
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
        "limit": 1,
    }


@pytest.mark.parametrize(
    "followup",
    [
        "继续看下一页",
        "那剩下的也给我看看",
        "还有别的订单吗",
    ],
)
def test_order_followups_keep_ordinal_without_cursor_override(followup: str) -> None:
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
    assert page_call.arguments == {"query": followup}


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
                            "limit": 20,
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
async def test_failed_run_uses_compact_context_audit(
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

    await runtime._mark_run_failed(state, "RuntimeError", "boom")

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
