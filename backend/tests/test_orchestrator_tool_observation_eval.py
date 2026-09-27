"""Twenty offline Tool observations through the production orchestration boundary."""

from dataclasses import dataclass
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.decisions import PlannedToolCall
from app.agent.graph import (
    AgentRuntime,
    _followup_tool_call_allowed,
    _state_terminal_decision,
)
from app.agent.state import AgentState
from app.core.config import Settings
from app.schemas.context import WorkingMemoryV2
from app.tools.contracts import ToolContract
from app.tools.schemas import ToolError, ToolExecutionResult

PRODUCT = {
    "spu_id": 10,
    "sku_id": 101,
    "title": "Test Keyboard",
    "brand": "Test",
    "category": "keyboard",
    "price": "399.00",
    "stock": 8,
    "sku_sales_count": 3,
    "sales_count": 12,
    "specs": {"connection_type": "Wireless", "switches": "静音红轴"},
}
SECOND_PRODUCT = {**PRODUCT, "spu_id": 11, "sku_id": 102, "title": "Second Keyboard"}
DOCUMENT = {
    "source_type": "knowledge_document",
    "source_id": 1,
    "title": "选购指南",
    "document_type": "guide",
    "snippet": "办公键盘可优先考虑低噪音轴体。",
}
ORDER = {
    "id": 202607210001,
    "status": 3,
    "status_label": "已发货",
    "pay_amount": "399.00",
    "created_at": "2026-07-21T10:00:00",
    "items": [],
    "logistics": None,
}


@dataclass(frozen=True)
class ObservationCase:
    case_id: str
    tool_name: str
    arguments: dict[str, Any]
    execution: ToolExecutionResult
    expected_outcome: str
    expected_status: str
    expected_terminal_type: str
    followup_arguments: dict[str, Any] | None = None
    expected_followup_allowed: bool = False


def _success(tool_name: str, output: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(tool_name=tool_name, ok=True, output=output)


def _error(tool_name: str, code: str, action: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name=tool_name,
        ok=False,
        error=ToolError(
            code=code,
            message=f"scripted {code}",
            retryable=action in {"retry_once", "replan_arguments"},
            recommended_action=action,
        ),
    )


CASES = [
    ObservationCase(
        "01-catalog-applied",
        "catalog_search",
        {"query": "办公键盘", "limit": 3},
        _success(
            "catalog_search",
            {
                "result_type": "products",
                "products": [PRODUCT],
                "query_plan": {"usage_mapping": {"status": "applied"}},
            },
        ),
        "usable",
        "ready_to_answer",
        "grounded_response",
    ),
    ObservationCase(
        "02-catalog-expanded",
        "catalog_search",
        {"query": "推荐办公外设", "limit": 3},
        _success(
            "catalog_search",
            {
                "result_type": "products",
                "products": [PRODUCT],
                "query_plan": {"usage_mapping": {"status": "expanded"}},
            },
        ),
        "usable",
        "ready_to_answer",
        "grounded_response",
    ),
    ObservationCase(
        "03-catalog-plain-products",
        "catalog_search",
        {"query": "无线键盘", "limit": 3},
        _success("catalog_search", {"result_type": "products", "products": [PRODUCT]}),
        "usable",
        "ready_to_answer",
        "grounded_response",
    ),
    ObservationCase(
        "04-catalog-empty",
        "catalog_search",
        {"query": "不存在的键盘", "limit": 3},
        _success("catalog_search", {"result_type": "empty", "products": []}),
        "empty",
        "unavailable",
        "unavailable_response",
    ),
    ObservationCase(
        "05-usage-mapping-unavailable",
        "catalog_search",
        {"query": "办公鼠标", "limit": 3},
        _success(
            "catalog_search",
            {
                "result_type": "empty",
                "products": [],
                "ranking_strategy": "unsupported_query",
                "query_plan": {
                    "supported": False,
                    "error_type": "usage_mapping_unavailable",
                    "usage_mapping": {"status": "unavailable"},
                },
                "diagnostics": [{"code": "usage_mapping_unavailable"}],
            },
        ),
        "empty",
        "unavailable",
        "unavailable_response",
    ),
    ObservationCase(
        "06-catalog-unsupported",
        "catalog_search",
        {"query": "鼠标三个月销量趋势", "limit": 3},
        _success(
            "catalog_search",
            {
                "result_type": "empty",
                "products": [],
                "diagnostics": [{"code": "unsupported_query"}],
            },
        ),
        "unsupported",
        "unavailable",
        "unavailable_response",
    ),
    ObservationCase(
        "07-catalog-invalid-plan",
        "catalog_search",
        {"query": "办公键盘", "limit": 3},
        _success(
            "catalog_search",
            {
                "result_type": "empty",
                "products": [],
                "diagnostics": [{"code": "invalid_catalog_plan"}],
            },
        ),
        "insufficient",
        "needs_replan",
        "unavailable_response",
    ),
    ObservationCase(
        "08-catalog-contract-mismatch",
        "catalog_search",
        {"query": "办公键盘", "limit": 3},
        _success("catalog_search", {"result_type": "empty", "products": [PRODUCT]}),
        "insufficient",
        "needs_replan",
        "unavailable_response",
    ),
    ObservationCase(
        "09-compare-two-products",
        "catalog_compare",
        {"query": "比较这两个具体 SKU 键盘版本", "limit": 5},
        _success(
            "catalog_compare",
            {"result_type": "comparison", "products": [PRODUCT, SECOND_PRODUCT]},
        ),
        "usable",
        "ready_to_answer",
        "grounded_response",
    ),
    ObservationCase(
        "10-compare-one-product",
        "catalog_compare",
        {"query": "比较这两个具体 SKU 键盘版本", "limit": 5},
        _success("catalog_compare", {"result_type": "comparison", "products": [PRODUCT]}),
        "insufficient",
        "needs_replan",
        "unavailable_response",
    ),
    ObservationCase(
        "11-compare-empty",
        "catalog_compare",
        {"query": "比较这两个具体 SKU 键盘版本", "limit": 5},
        _success("catalog_compare", {"result_type": "comparison", "products": []}),
        "empty",
        "unavailable",
        "unavailable_response",
    ),
    ObservationCase(
        "12-facets-usable",
        "catalog_facets",
        {"query": "有哪些键盘品牌", "limit": 20},
        _success(
            "catalog_facets",
            {"result_type": "facets", "items": [{"value": "Test", "count": 2}]},
        ),
        "usable",
        "ready_to_answer",
        "grounded_response",
    ),
    ObservationCase(
        "13-facets-empty",
        "catalog_facets",
        {"query": "有哪些轨迹球品牌", "limit": 20},
        _success("catalog_facets", {"result_type": "facets", "items": []}),
        "empty",
        "unavailable",
        "unavailable_response",
    ),
    ObservationCase(
        "14-order-single",
        "order_lookup",
        {"order_id": 202607210001, "limit": 1},
        _success("order_lookup", {"result_type": "single_order", "order": ORDER}),
        "usable",
        "ready_to_answer",
        "grounded_response",
    ),
    ObservationCase(
        "15-order-candidates",
        "order_lookup",
        {"query": "我的最近订单", "limit": 5},
        _success(
            "order_lookup",
            {"result_type": "order_candidates", "candidates": [ORDER]},
        ),
        "usable",
        "ready_to_answer",
        "grounded_response",
    ),
    ObservationCase(
        "16-order-not-found",
        "order_lookup",
        {"order_id": 202607219999, "limit": 1},
        _success("order_lookup", {"result_type": "not_found", "candidates": []}),
        "not_found",
        "unavailable",
        "unavailable_response",
    ),
    ObservationCase(
        "17-policy-document",
        "policy_search",
        {"query": "退货政策", "limit": 5},
        _success("policy_search", {"result_type": "documents", "documents": [DOCUMENT]}),
        "usable",
        "ready_to_answer",
        "grounded_response",
    ),
    ObservationCase(
        "18-knowledge-empty",
        "knowledge_search",
        {"query": "未知品牌介绍", "limit": 5},
        _success("knowledge_search", {"result_type": "documents", "documents": []}),
        "empty",
        "unavailable",
        "unavailable_response",
    ),
    ObservationCase(
        "19-timeout-recovery",
        "catalog_search",
        {"query": "办公键盘", "limit": 3},
        _error("catalog_search", "timeout", "retry_once"),
        "error",
        "failed",
        "unavailable_response",
        followup_arguments={"query": "办公键盘", "limit": 3},
        expected_followup_allowed=True,
    ),
    ObservationCase(
        "20-invalid-input-recovery",
        "catalog_search",
        {"query": "办公键盘", "limit": 3},
        _error("catalog_search", "invalid_input", "replan_arguments"),
        "error",
        "failed",
        "unavailable_response",
        followup_arguments={"query": "办公键盘", "limit": 5},
        expected_followup_allowed=True,
    ),
]


class ScriptedToolExecutor:
    def __init__(self, execution: ToolExecutionResult):
        self.execution = execution
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult:
        self.calls.append((contract.llm_name, arguments, runtime_context))
        return self.execution


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
@pytest.mark.asyncio
async def test_20_scripted_tool_observations_through_orchestrator(
    case: ObservationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConversationRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr("app.agent.graph.ConversationRepository", FakeConversationRepository)
    monkeypatch.setattr("app.agent.graph.get_stream_writer", lambda: lambda event: None)
    executor = ScriptedToolExecutor(case.execution)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        tool_executor=executor,
    )
    subquery = f"处理场景 {case.case_id}"
    state = cast(
        AgentState,
        {
            "user_id": 7,
            "run_id": 61,
            "message": subquery,
            "decision": {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": case.case_id,
                        "name": case.tool_name,
                        "arguments": case.arguments,
                        "subquery": subquery,
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

    await runtime._execute_tool_wave(state)
    await runtime._normalize_tool_results(state)
    await runtime._update_subquery_ledger(state)

    assert len(executor.calls) == 1
    assert executor.calls[0][0] == case.tool_name
    expected_runtime = {"user_id": 7}
    actual_runtime = executor.calls[0][2]
    if case.tool_name in {"catalog_search", "catalog_compare", "catalog_facets"}:
        assert actual_runtime["user_id"] == 7
        assert actual_runtime["catalog_context"]["dependency_artifacts"] == []
        assert actual_runtime["catalog_context"]["memory"] == (
            WorkingMemoryV2().catalog.model_dump(mode="json")
        )
    else:
        assert actual_runtime == expected_runtime
    assert state["normalized_tool_results"][0]["outcome"] == case.expected_outcome
    assert state["subquery_ledger"][0]["status"] == case.expected_status

    terminal = _state_terminal_decision(state, "offline_observation_eval")
    assert terminal.type == case.expected_terminal_type

    followup = PlannedToolCall(
        id=f"{case.case_id}-followup",
        name=case.tool_name,
        arguments=case.followup_arguments or case.arguments,
        subquery=subquery,
    )
    assert _followup_tool_call_allowed(state, followup) is case.expected_followup_allowed


def test_tool_observation_eval_contains_exactly_20_cases() -> None:
    assert len(CASES) == 20
    assert len({case.case_id for case in CASES}) == 20
