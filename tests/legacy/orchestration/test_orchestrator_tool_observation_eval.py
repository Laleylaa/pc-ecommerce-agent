"""Planned tool execution to task/answer handoff, with isolated executors."""

from dataclasses import dataclass
from typing import Any

import pytest
from app.agent.legacy.decisions import PlannedToolCall
from app.agent.legacy.runtime import (
    _followup_tool_call_allowed,
    _state_terminal_decision,
)
from app.schemas.catalog_search import CatalogSearchResult
from app.tools.catalog_search.outcomes import policy_for
from app.tools.contracts import ToolContract
from app.tools.schemas import ToolError, ToolExecutionResult
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.planning_harness import goal, plan_request, task

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
    "candidate_sku_ids": [101],
    "model_sku_count": 1,
}
DOCUMENT = {
    "source_type": "knowledge_document",
    "source_id": 1,
    "title": "退货政策",
    "document_type": "policy",
    "snippet": "退货申请需确认订单及商品状态。",
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


def _search(reason: str, products: list | None = None) -> ToolExecutionResult:
    """Current schema-valid result; expected outcomes are independently specified below."""
    policy = policy_for(reason)
    fields = dict(policy.fields(), result_type="products" if products else "empty",
                  products=products or [], diagnostics=[policy.diagnostic()],
                  query_state=policy.query_states[0])
    if fields["query_state"] == "completed":
        count = len(products or [])
        fields.update(
            match_kind="exact" if count else "none", returned_count=count,
            candidate_sku_count=count, candidate_spu_count=count, candidate_count=count,
            total_match_count=count, matched_spu_count=count, matched_sku_count=count,
            unknown_sku_count=0, conflicted_sku_count=0, missing_fact_sku_count=0,
            matching_complete=True, is_exhaustive=True, data_quality="clear",
            selection_scope="sku", offset=0, limit=3,
        )
    result = CatalogSearchResult.model_validate(fields).model_dump(mode="json")
    return _success("catalog_search", result)


CASES = [
    ObservationCase(
        "03-catalog-plain-products",
        "catalog_search",
        {"query": "无线键盘", "limit": 3},
        _search("matches_found", [PRODUCT]),
        "usable",
        "ready_to_answer",
        "partial_response",
    ),
    ObservationCase(
        "04-catalog-empty",
        "catalog_search",
        {"query": "不存在的键盘", "limit": 3},
        _search("no_matching_sku"),
        "empty",
        "unavailable",
        "grounded_response",
    ),
    ObservationCase(
        "05-requirement-needs-clarification",
        "catalog_search",
        {"query": "办公鼠标", "limit": 3},
        _search("requirement_needs_clarification"),
        "insufficient",
        "needs_replan",
        "clarification",
    ),
    ObservationCase(
        "06-catalog-unsupported",
        "catalog_search",
        {"query": "鼠标三个月销量趋势", "limit": 3},
        _search("request_not_supported"),
        "unsupported",
        "unavailable",
        "unavailable_response",
    ),
    ObservationCase(
        "07-catalog-invalid-plan",
        "catalog_search",
        {"query": "办公键盘", "limit": 3},
        _search("invalid_plan"),
        "error",
        "failed",
        "unavailable_response",
    ),
    ObservationCase(
        "08-catalog-contract-mismatch",
        "catalog_search",
        {"query": "办公键盘", "limit": 3},
        _error("catalog_search", "execution_error", "stop"),
        "error",
        "failed",
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
        "partial_response",
    ),
    ObservationCase(
        "13-facets-empty",
        "catalog_facets",
        {"query": "有哪些轨迹球品牌", "limit": 20},
        _success("catalog_facets", {"result_type": "facets", "items": []}),
        "empty",
        "unavailable",
        "grounded_response",
    ),
    ObservationCase(
        "14-order-single",
        "order_lookup",
        {"order_id": 202607210001, "limit": 1},
        _success("order_lookup", {"result_type": "single_order", "order": ORDER}),
        "usable",
        "ready_to_answer",
        "partial_response",
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
        "partial_response",
    ),
    ObservationCase(
        "16-order-not-found",
        "order_lookup",
        {"order_id": 202607219999, "limit": 1},
        _success("order_lookup", {"result_type": "not_found", "candidates": []}),
        "not_found",
        "unavailable",
        "grounded_response",
    ),
    ObservationCase(
        "17-policy-document",
        "policy_search",
        {"query": "退货政策", "limit": 5},
        _success("policy_search", {"result_type": "documents", "documents": [DOCUMENT]}),
        "usable",
        "ready_to_answer",
        "partial_response",
    ),
    ObservationCase(
        "18-knowledge-empty",
        "knowledge_search",
        {"query": "未知品牌介绍", "limit": 5},
        _success("knowledge_search", {
            "result_type": "documents", "documents": [], "search_strategy": "web_search",
        }),
        "insufficient",
        "needs_replan",
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
        expected_followup_allowed=False,
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
async def test_scripted_tool_observations_through_orchestrator(
    case: ObservationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConversationRepository:
        def __init__(self, session: AsyncSession):
            pass

        async def add_tool_call(self, *args: Any) -> None:
            pass

    monkeypatch.setattr(
        "app.agent.legacy.runtime.ConversationRepository", FakeConversationRepository,
    )
    monkeypatch.setattr("app.agent.legacy.runtime.get_stream_writer", lambda: lambda event: None)
    executor = ScriptedToolExecutor(case.execution)
    subquery = str(case.arguments.get("query") or f"查询订单{case.arguments['order_id']}")
    runtime, state, _, _, _ = await plan_request(
        subquery, [goal(subquery)],
        [task(subquery, tool_name=case.tool_name, arguments=case.arguments)],
    )
    runtime.tool_executor = executor
    state["run_id"] = 61
    await runtime._orchestrate(state)
    await runtime._execute_tool_wave(state)
    await runtime._normalize_tool_results(state)
    await runtime._extract_task_artifacts(state)
    await runtime._update_subquery_ledger(state)

    expected_attempts = 2 if case.case_id == "19-timeout-recovery" else 1
    assert len(executor.calls) == expected_attempts
    for name, arguments, actual_runtime in executor.calls:
        assert name == case.tool_name
        for key, value in case.arguments.items():
            assert arguments[key] == value
        assert actual_runtime == {"user_id": 7}
    if expected_attempts == 2:
        assert executor.calls[0] == executor.calls[1]
        assert state["tool_wave_count"] == 1  # Retry is inside the same wave, then exhausted.
        assert state["subquery_ledger"][0]["status"] == "superseded"
    assert state["normalized_tool_results"][0]["outcome"] == case.expected_outcome
    assert state["subquery_ledger"][-1]["status"] == case.expected_status

    terminal = _state_terminal_decision(state, "offline_observation_eval")
    assert terminal.type == case.expected_terminal_type
    if case.expected_outcome == "usable":
        assert terminal.used_tool_call_ids == ["planned_task_1"]
        assert state["task_status"]["task_1"]["status"] == "succeeded"
        assert state["task_artifacts"]["task_1"]["usable"] is True
    if case.case_id == "18-knowledge-empty":
        assert state["normalized_tool_results"][0]["reason"] == "no_retrieval_evidence"
        assert "不代表" in terminal.response

    followup = PlannedToolCall(
        id=f"{case.case_id}-followup",
        name=case.tool_name,
        arguments=case.followup_arguments or case.arguments,
        subquery="task_1",
    )
    assert _followup_tool_call_allowed(state, followup) is case.expected_followup_allowed
