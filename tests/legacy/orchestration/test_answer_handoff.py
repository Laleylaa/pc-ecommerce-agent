import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.agent.legacy.answer_context import build_answer_context
from app.agent.legacy.prompts.observation import build_answer_prompt
from app.schemas.chat import ChatRequest
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog_search.outcomes import ResultReason, policy_for
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.orders import LLMOrderQueryPlanner, OrderToolService
from app.tools.schemas import ToolError, ToolExecutionResult

from tests.support.answer_state import (
    result_state,
)
from tests.support.runtime import goal, message, setup_runtime, task
from tests.support.tool_planning import Model, Repository


@pytest.mark.parametrize("reason,handling,semantic,control", [
    ("invalid_plan", "answer", "execution_failed", "finish_unavailable"),
    ("data_insufficient", "answer", "insufficient_evidence", "finish_unavailable"),
    ("no_matching_sku", "answer", "answered_no_match", "finish_answer"),
    ("target_not_found", "answer", "answered_no_match", "finish_answer"),
    ("request_not_supported", "answer", "unsupported_capability", "finish_unavailable"),
    ("target_inactive", "answer", "insufficient_evidence", "finish_unavailable"),
    ("target_outside_scope", "answer", "insufficient_evidence", "finish_unavailable"),
    ("query_needs_clarification", "clarify", "needs_clarification", "ask_clarification"),
    ("requirement_needs_clarification", "clarify", "needs_clarification", "ask_clarification"),
    ("conflicting_conditions", "clarify", "needs_clarification", "ask_clarification"),
    ("multiple_targets", "suggest_recovery", "execution_failed", "finish_unavailable"),
])
def test_search_stops_keep_reason_and_choose_three_handling_directions(
    reason, handling, semantic, control,
):
    policy = policy_for(reason)
    # Result-projection fixtures use the public policy's status and valid query phase.
    # This test does not exercise the search plan/schema's conflict-evidence validation.
    output = {
        "contract_version": "3", "result_type": "empty", **policy.fields(),
        "query_state": policy.query_states[0], "diagnostics": [policy.diagnostic()],
    }
    state = result_state("catalog_search", output)
    context = build_answer_context(state)
    item = context["tasks"][0]
    assert item["handling"] == handling
    assert item["semantic_outcome"] == semantic
    assert item["result"]["reason_code"] == reason
    assert item["artifact"]["facts"]["reason_code"] == reason
    assert context["recommended_control_action"] == control
    assert context["recovery_execution_enabled"] is False


@pytest.mark.parametrize("code,action,retryable,handling", [
    ("planning_output_invalid", "stop", False, "answer"),
    ("planning_call_timeout", "stop", False, "answer"),
    ("execution_error", "stop", False, "answer"),
    ("web_search_timeout", "stop", False, "answer"),
    ("web_search_not_configured", "stop", False, "answer"),
    ("forbidden", "stop", False, "answer"),
    ("unauthorized", "request_authentication", False, "answer"),
    ("timeout", "retry_once", True, "answer"),
    ("invalid_input", "replan_arguments", True, "suggest_recovery"),
    ("invalid_input", "stop", False, "answer"),
])
def test_execution_errors_never_become_missing_product_facts(code, action, retryable, handling):
    state = result_state("catalog_search", error={
        "code": code, "message": "synthetic internal error", "retryable": retryable,
        "recommended_action": action,
    })
    item = build_answer_context(state)["tasks"][0]
    assert item["handling"] == handling
    assert item["semantic_outcome"] in {"execution_failed", "temporarily_unavailable"}
    assert item["result"]["reason_code"] == code
    assert "synthetic internal error" not in json.dumps(item)
    assert state["task_status"]["task_1"]["status"] == "failed"


def test_recovery_advice_does_not_reset_wave_budget():
    state = result_state("catalog_search", waves=2, error={
        "code": "invalid_input", "retryable": True, "recommended_action": "replan_arguments",
    })
    assert build_answer_context(state)["tasks"][0]["handling"] == "answer"


@pytest.mark.parametrize("reason,action", [
    ("product_identity_required", "resolve_product_with_catalog_search"),
    ("multiple_targets", "split_tasks"),
])
def test_facets_delegates_missing_search_or_split_to_planning_not_user(reason, action):
    output = {
        "result_type": "empty", "items": [],
        "diagnostics": [{"code": reason, "recommended_action": action}],
    }
    item = build_answer_context(result_state("catalog_facets", output))["tasks"][0]
    assert item["handling"] == "suggest_recovery"
    assert item["semantic_outcome"] == "execution_failed"
    assert item["recovery"]["target_stage"] == "llm2"
    assert item["recovery"]["execution_enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets", "order_lookup"])
async def test_real_planner_exhaustion_reaches_answer_without_new_retry(tool, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Exhausted planning must not query a repository")

    monkeypatch.setattr("app.repositories.catalog_search.CatalogSearchRepository.load", forbidden)
    monkeypatch.setattr("app.repositories.orders.OrderRepository.count_orders", forbidden)
    model, repository = Model("not JSON", "still not JSON", "again not JSON"), Repository()
    handlers = {
        "catalog_search": CatalogSearchService(repository, CatalogSearchPlanner(model)).search,
        "catalog_facets": CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets,
        "order_lookup": OrderToolService(None, planner=LLMOrderQueryPlanner(model)).lookup,
    }
    execution = await execute_bound_tool(
        BoundTool(DefaultToolContractProvider().get_contract(tool), handlers[tool]),
        {"query": "去年有几笔订单" if tool == "order_lookup" else "白色键盘有哪些布局"},
        {"user_id": 7},
    )
    assert not execution.ok and execution.error.code == "planning_output_invalid"
    state = result_state(tool, error=execution.error.model_dump(mode="json"))
    item = build_answer_context(state)["tasks"][0]
    assert item["handling"] == "answer"
    assert item["semantic_outcome"] == "execution_failed"
    assert item["result"]["reason_code"] == "planning_output_invalid"
    assert item["result"]["recommended_action"] == "stop"
    assert not repository.calls and len(model.calls) == 3


@pytest.mark.parametrize("tool", ["policy_search", "knowledge_search"])
def test_empty_retrieval_is_not_a_negative_business_fact(tool):
    item = build_answer_context(result_state(tool, {"result_type": "empty", "documents": []}))
    assert item["completion"] == "none"
    assert item["tasks"][0]["handling"] == "answer"
    assert item["tasks"][0]["result"]["reason_code"] == "no_retrieval_evidence"
    assert item["tasks"][0]["semantic_outcome"] == "insufficient_evidence"


@pytest.mark.parametrize("items", [[], [{"value": "style", "count": 1, "values": [
    {"value": "75%", "sku_ids": [1], "spu_ids": [10]},
]}]])
def test_facets_keep_scope_missing_fields_and_version_ownership(items):
    output = {
        "result_type": "facets" if items else "empty", "facet": "spec_key", "items": items,
        "scope": {"spu_ids": [10]}, "total_value_count": len(items), "returned_count": len(items),
        "is_exhaustive": False, "matching_complete": False,
        "field_issues": [{"field": "specs", "status": "missing", "sku_id": 2}],
        "diagnostics": [{"code": "data_insufficient", "message": "部分规格资料缺失"}],
    }
    context = build_answer_context(result_state("catalog_facets", output))
    item = context["tasks"][0]
    assert item["artifact"]["facts"] == output
    assert item["handling"] == "answer"
    assert item["semantic_outcome"] == (
        "answered_with_facts" if items else "insufficient_evidence"
    )


@pytest.mark.parametrize("tool", ["catalog_facets", "order_lookup"])
def test_conflicting_conditions_reach_clarification_instead_of_empty_or_unsupported(tool):
    output = {
        "result_type": "invalid_query" if tool == "order_lookup" else "empty",
        "query_plan": {"supported": False},
        "diagnostics": [{"code": "conflicting_conditions", "message": "价格上下界冲突"}],
    }
    context = build_answer_context(result_state(tool, output))
    assert context["tasks"][0]["handling"] == "clarify"
    assert context["tasks"][0]["result"]["diagnostics"][0]["message"] == "价格上下界冲突"


def test_failed_prerequisite_is_not_downstream_missing_facts():
    state = result_state(
        "catalog_search", error={"code": "planning_output_invalid"}, dependency=True,
    )
    tasks = build_answer_context(state)["tasks"]
    assert tasks[1]["semantic_outcome"] == "blocked_dependency"
    assert tasks[1]["dependencies"] == [{
        "task_id": "task_1", "status": "failed", "reason": "tool_outcome:error",
        "result_reason": "tool_execution_failed:planning_output_invalid",
    }]
    assert tasks[1]["result"]["query_state"] == "unknown"
    state["route_plan"]["subqueries"][0]["tasks"][0]["answer_role"] = "internal"
    context = build_answer_context(state)
    assert context["supporting_source_tool_call_ids"] == []
    assert context["supporting_tasks"][0]["partial_answer_goal_ids"] == []
    assert context["tasks"][0]["semantic_outcome"] == "blocked_dependency"


def test_zero_orders_is_a_valid_count_and_bounded_analysis_stays_bounded():
    context = build_answer_context(result_state("order_lookup", {
        "result_type": "order_count", "query_mode": "count", "total_match_count": 0,
    }))
    assert context["completion"] == "full"
    context = build_answer_context(result_state("order_lookup", {
        "result_type": "order_analysis", "query_mode": "analysis",
        "analysis_orders": [{"id": 7}], "is_exhaustive": False, "total_match_count": 12,
    }))
    assert context["tasks"][0]["artifact"]["facts"]["is_exhaustive"] is False
    assert any("不完整" in s for s in context["tasks"][0]["response_contract"]["required"])


@pytest.mark.asyncio
async def test_full_graph_merges_tool_success_failure_and_non_tool_goals(monkeypatch):
    queries = ["查询SKU 1", "查询SKU 2", "刷新率是什么意思", "推荐办公设备", "取消订单"]
    goals = [goal(queries[0]), goal(queries[1], "goal_2")]
    goals += [goal(q, f"goal_{i}", disposition=d) for i, q, d in [
        (3, queries[2], "direct_response"), (4, queries[3], "clarification"),
        (5, queries[4], "human_handoff"),
    ]]
    runtime, _, executor, context = setup_runtime(monkeypatch, "；".join(queries), goals, [
        {"goal_id": "goal_1", "tasks": [task(queries[0])]},
        {"goal_id": "goal_2", "tasks": [task(queries[1], identity="task_2")]},
    ])
    original = executor.execute

    async def execute(contract, arguments, trusted):
        if "2" in arguments["query"]:
            return ToolExecutionResult(tool_name=contract.name, ok=False, error=ToolError(
                code="planning_output_invalid", message="scripted exhaustion",
            ))
        return await original(contract, arguments, trusted)

    executor.execute = execute
    response = "第一款200元；第二款暂未查成。刷新率是每秒刷新次数。办公更在意什么？取消需人工处理。"
    answer = AsyncMock(return_value=message("finish_partial", {
        "response": response, "used_tool_call_ids": ["planned_task_1"],
        "unavailable_parts": [queries[1], queries[3]],
        "covered_goal_ids": [g["id"] for g in goals],
    }))
    runtime.answer_synthesizer = SimpleNamespace(ainvoke=answer)
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message="；".join(queries)), 1)
    )
    sent = answer.call_args.args[0]
    payload_text = sent[-1].content.split("<answer_context>\n")[1].split("\n</answer_context>")[0]
    payload = json.loads(payload_text)
    assert len(payload["goals"]) == 5
    assert payload["goals"][0]["task_ids"] == ["task_1"]
    assert [g["handling"] for g in payload["goals"][2:]] == ["answer", "clarify", "answer"]
    assert payload["tasks"][1]["semantic_outcome"] == "execution_failed"
    assert state["terminal_guard_status"] == "accepted"
    assert response in state["answer"] and context.completed == 1
    assert sent[0].content == build_answer_prompt(recovery_enabled=True)


@pytest.mark.asyncio
async def test_no_tool_goals_use_same_composed_prompt_and_history(monkeypatch):
    query = "总结刚才的说明，再介绍一下刷新率"
    runtime, model, executor, _ = setup_runtime(monkeypatch, query, [
        goal(query, disposition="direct_response"),
    ], [])
    state = runtime._initial_state(ChatRequest(message=query), 1)
    state["route_plan"] = {"rewritten_query": query, "subqueries": [{
        "id": "goal_1", "query": query, "disposition": "session_grounded_response",
        "reason_code": "test", "tasks": [],
    }]}
    state["history"] = [{"role": "assistant", "content": "刷新率的单位是Hz。"}]
    state["boundary"] = {"classification": "in_scope_auto"}
    await runtime._summarize_goals(state)
    sent = model.calls["summarize_goals"][0]
    assert sent[0].content == build_answer_prompt(output_mode="summary", recovery_enabled=True)
    payload = json.loads(sent[1].content)
    assert payload["answer_context"]["history"] == state["history"]
    assert payload["answer_context"]["goals"][0]["handling"] == "answer"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_tool_clarification_preserves_independent_success_in_partial_answer(monkeypatch):
    queries = ["查询SKU 1", "推荐办公键盘"]
    runtime, _, executor, _ = setup_runtime(monkeypatch, "；".join(queries), [
        goal(queries[0]), goal(queries[1], "goal_2"),
    ], [
        {"goal_id": "goal_1", "tasks": [task(queries[0])]},
        {"goal_id": "goal_2", "tasks": [task(queries[1], identity="task_2")]},
    ])
    original = executor.execute

    async def execute(contract, arguments, trusted):
        if arguments["query"] == queries[1]:
            output = CatalogSearchService._stop(ResultReason.REQUIREMENT_NEEDS_CLARIFICATION)
            return ToolExecutionResult(
                tool_name=contract.name, ok=True, output=output.model_dump(mode="json"),
            )
        return await original(contract, arguments, trusted)

    executor.execute = execute
    answer = AsyncMock(return_value=message("finish_partial", {
        "response": "这款键盘200元。办公键盘对尺寸或连接方式有什么要求？",
        "used_tool_call_ids": ["planned_task_1"], "unavailable_parts": [queries[1]],
    }))
    runtime.answer_synthesizer = SimpleNamespace(ainvoke=answer)
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message="；".join(queries)), 7),
    )
    context = state["answer_context"]
    assert [t["handling"] for t in context["tasks"]] == ["answer", "clarify"]
    assert context["recommended_control_action"] == "finish_partial"
    assert context["tasks"][1]["missing_information"]
    assert state["terminal_guard_status"] == "accepted" and answer.await_count == 1
