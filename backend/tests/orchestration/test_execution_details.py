"""Request statistics and UI projection, isolated from model and database services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.execution_details import build_execution_details
from app.core.llm_calls import invoke_before
from app.core.model_usage import collect_model_calls, model_call_counts
from app.schemas.chat import ChatRequest
from tests.support.runtime import goal, message, setup_runtime, task


@pytest.mark.asyncio
async def test_model_counts_include_failed_attempts_and_isolate_concurrent_requests():
    async def request(stage, attempts):
        with collect_model_calls():
            model = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("failed")))

            async def child():
                with pytest.raises(RuntimeError):
                    await invoke_before(
                        model, [], asyncio.get_running_loop().time() + 5, stage=stage,
                    )

            await asyncio.gather(*(child() for _ in range(attempts)))
            return model_call_counts()

    assert await asyncio.gather(request("answer", 2), request("tool_planning", 3)) == [
        {"answer": 2}, {"tool_planning": 3},
    ]
    assert model_call_counts() is None
    with collect_model_calls():
        with pytest.raises(TimeoutError):
            await invoke_before(SimpleNamespace(), [], 0, stage="answer")
        assert model_call_counts() == {}  # Expired budget never reached the model.


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
async def test_tool_internal_repair_counts_each_model_attempt():
    from langchain_core.messages import AIMessage
    from pydantic import BaseModel

    from app.tools.structured_planning import generate_tool_plan

    class Plan(BaseModel):
        value: int

    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
        AIMessage(content="not json"), AIMessage(content='{"value": 7}'),
    ]))
    with collect_model_calls():
        plan = await generate_tool_plan(
            model=model, schema=Plan, required=("value",), prompt="plan", payload={},
        )
        assert plan.value == 7
        assert model_call_counts() == {"tool_planning": 2}


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
