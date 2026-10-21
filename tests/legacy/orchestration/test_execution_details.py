
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.agent.legacy.execution_details import build_execution_details
from app.core.model_usage import model_call_counts
from app.schemas.chat import ChatRequest

from tests.support.runtime import goal, message, setup_runtime, task


def test_projection_keeps_failed_replaced_and_reused_records_without_private_payloads():
    entries = [
        {"tool_call_id": "first", "subquery": "task_1", "tool_name": "catalog_search",
         "outcome": "error", "reason": "timeout", "status": "superseded"},
        {"tool_call_id": "second", "subquery": "task_1", "tool_name": "catalog_search",
         "outcome": "usable", "reason": "ok", "status": "answered",
         "arguments": {"query": "键盘", "user_id": 999, "api_key": "hidden"}},
        {"tool_call_id": "reuse", "subquery": "task_2", "tool_name": "catalog_search",
         "outcome": "usable", "reason": "ok", "reused_from_tool_call_id": "second"},
    ]
    state = {
        "route_plan": {"subqueries": [{"id": "goal_1", "query": "键盘",
                        "disposition": "tool_planning", "tasks": [{"id": "task_1"}]}]},
        "subquery_ledger": entries,
        "decision": {"used_tool_call_ids": ["second"]},
        "tool_waves": [{"results": [{"tool_call_id": "second", "execution": {
            "output": {"returned_count": 1, "query_plan": {"secret": "hidden"},
                       "products": [{"title": "测试键盘", "internal": "hidden"}]},
        }}]}],
    }
    details = build_execution_details(state)
    assert details.model_calls is None
    assert details.goals[0].task_ids == ["task_1"]
    first, second, reuse = details.tool_calls
    assert first.superseded and first.outcome == "error"
    assert second.used_in_answer and not first.used_in_answer
    assert reuse.reused
    assert second.arguments == {"query": "键盘"}
    assert second.result_summary == {"returned_count": 1, "products": [{"title": "测试键盘"}]}
    assert "hidden" not in details.model_dump_json()


@pytest.mark.asyncio
async def test_runtime_returns_and_persists_same_execution_details(monkeypatch):
    text = "查询测试键盘1"
    runtime, _, _, context = setup_runtime(
        monkeypatch, text, [goal(text)],
        [{"goal_id": "goal_1", "tasks": [task(text)]}],
        [{"operation": "lookup", "purpose": "lookup", "name": "测试键盘1"}],
    )
    runtime.answer_synthesizer = SimpleNamespace(ainvoke=AsyncMock(return_value=message(
        "finish_answer", {"response": "测试键盘1售价200元。",
                          "used_tool_call_ids": ["planned_task_1"], "covered_goal_ids": ["goal_1"]},
    )))
    saved = []
    complete = context.complete_turn

    async def capture(prepared, state):
        saved.append(state["assistant_metadata"]["execution_details"])
        return await complete(prepared, state)

    context.complete_turn = capture
    response = (await runtime.run(ChatRequest(message=text), 1)).model_dump(mode="json")
    assert response["execution_details"] == saved[0]
    details = response["execution_details"]
    assert details["goals"][0]["task_ids"] == ["task_1"]
    assert details["tool_calls"][0]["tool_name"] == "catalog_search"
    assert details["tool_calls"][0]["result_summary"]["returned_count"] == 1
    assert details["model_calls"]["route_request"] == 1
    assert details["model_calls"]["submit_execution_plan"] == 1
    assert details["model_calls"]["answer"] == 1
    assert details["tool_calls"][0]["used_in_answer"]
    assert "tool_planning" not in details["model_calls"]  # Fixed tool plan uses no model.
    assert model_call_counts() is None


@pytest.mark.asyncio
async def test_no_tool_response_does_not_invent_tool_calls(monkeypatch):
    text = "解释刷新率"
    runtime, _, _, _ = setup_runtime(
        monkeypatch, text, [goal(text, disposition="direct_response")], [],
    )
    response = await runtime.run(ChatRequest(message=text), 1)
    details = response.execution_details
    assert details.goals[0].disposition == "direct_response"
    assert details.tool_calls == []
    assert details.model_calls == {"route_request": 1, "summarize_goals": 1}
