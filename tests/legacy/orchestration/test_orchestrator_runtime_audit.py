from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.agent.legacy.answer_context import build_answer_context
from app.agent.legacy.decisions import decision_from_ai_message
from app.agent.legacy.tool_loop import _deterministic_recovery_decision
from app.schemas.chat import ChatRequest
from app.tools.schemas import ToolError, ToolExecutionResult
from langchain_core.messages import AIMessage

from tests.support.runtime import goal, message, setup_runtime, task
from tests.support.runtime_audit import (
    invoke,
    single,
)


@pytest.mark.asyncio
async def test_fixed_new_plan_timeout_retry_stops_after_two_attempts(monkeypatch):
    runtime, _, executor, context = single(monkeypatch)
    executor.execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_name="catalog_search",
            ok=False,
            error=ToolError(
                code="timeout",
                message="scripted timeout",
                retryable=True,
                recommended_action="retry_once",
            ),
        )
    )
    state = await invoke(runtime, "查询键盘")
    assert executor.execute.await_count == 2
    assert state["tool_wave_count"] == 1
    assert state["task_status"]["task_1"]["status"] == "failed"
    assert _deterministic_recovery_decision(state) is None
    assert context.completed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_mode", ["fallback", "model", "plain_text"])
async def test_answer_fallback_preserves_both_searches_without_claiming_full_completion(
    monkeypatch, answer_mode
):
    first, second = "查询SKU 1", "查询SKU 2"
    runtime, _, _, context = setup_runtime(
        monkeypatch,
        first + "；" + second,
        [goal(first), goal(second, identity="goal_2")],
        [
            {
                "goal_id": "goal_1",
                "tasks": [
                    task(
                        first,
                        arguments={
                            "query": first,
                            "scope": {"sku_ids": [1]},
                        },
                    )
                ],
            },
            {
                "goal_id": "goal_2",
                "tasks": [
                    task(
                        second,
                        identity="task_2",
                        arguments={
                            "query": second,
                            "scope": {"sku_ids": [2]},
                        },
                    )
                ],
            },
        ],
        [{"category": "键盘", "category_evidence": "键盘"}] * 2,
    )
    if answer_mode == "fallback":
        model = AsyncMock(side_effect=RuntimeError("offline"))
    elif answer_mode == "plain_text":
        model = AsyncMock(return_value=AIMessage(content="测试键盘2售价250元。"))
    else:
        model = AsyncMock(
            return_value=message(
                "finish_answer",
                {
                    "response": "测试键盘2售价250元。",
                    "used_tool_call_ids": ["planned_task_2"],
                },
            )
        )
    runtime.answer_synthesizer = SimpleNamespace(ainvoke=model)
    state = await invoke(runtime, first + "；" + second)
    assert build_answer_context(state)["completion"] == "full"
    assert state["decision"]["control_action"] == "finish_partial"
    assert len(state["decision"]["used_tool_call_ids"]) == 2
    assert state["terminal_guard_status"] == (
        "accepted" if answer_mode == "fallback" else "fallback"
    )
    assert "测试键盘2" in state["answer"]
    assert "测试键盘1" in state["answer"]
    assert context.completed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_call", [1, 2])
async def test_audit_one_success_one_failure_keeps_independent_result(monkeypatch, failing_call):
    text = "查询便宜键盘；查询蓝牙键盘"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [goal("查询便宜键盘"), goal("查询蓝牙键盘", identity="goal_2")],
        [
            {"goal_id": "goal_1", "tasks": [task("查询便宜键盘")]},
            {"goal_id": "goal_2", "tasks": [task("查询蓝牙键盘", identity="task_2")]},
        ],
        [{"category": "键盘", "category_evidence": "键盘"}],
    )
    original = executor.execute
    count = 0

    async def execute(contract, arguments, runtime_context):
        nonlocal count
        count += 1
        if count == failing_call:
            raise RuntimeError("scripted independent tool failure")
        return await original(contract, arguments, runtime_context)

    executor.execute = execute
    state = await invoke(runtime, text)
    assert count == 2
    assert state["task_status"][f"task_{3 - failing_call}"]["status"] == "succeeded"
    assert state["task_status"][f"task_{failing_call}"]["status"] == "failed"
    assert state["decision"]["control_action"] == "finish_partial"
    assert "测试键盘" in state["answer"]


@pytest.mark.asyncio
async def test_answer_format_attempts_are_counted_and_do_not_restart_at_guard(monkeypatch):
    runtime, _, executor, _ = single(monkeypatch)
    executor.execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_name="catalog_search",
            ok=False,
            error=ToolError(code="internal_error", message="x"),
        )
    )
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="")))
    runtime.answer_synthesizer = model
    state = await invoke(runtime, "查询键盘")
    assert model.ainvoke.await_count == 2
    assert state["orchestrator_call_count"] == 2
    assert state["answer_call_count"] == 2
    assert "terminal_guard_replan_count" not in state
    assert state["terminal_guard_status"] == "fallback"


@pytest.mark.asyncio
async def test_summary_rejects_whitespace_before_persistence(monkeypatch):
    text = "请介绍一下你可以帮助我的事情"
    runtime, model, _, context = setup_runtime(
        monkeypatch,
        text,
        [goal(text, disposition="direct_response")],
        [],
    )
    model.scripts["summarize_goals"] = [message("summarize_goals", {"response": "   "})]
    state = await invoke(runtime, text)
    assert state["answer"].strip()
    assert state["terminal_guard_status"] == "fallback"
    assert context.completed == 1


def test_answer_parser_rejects_valid_and_invalid_tool_calls_together():
    response = message("finish_answer", {"response": "结果", "used_tool_call_ids": ["known"]})
    response.invalid_tool_calls = [
        {
            "name": "catalog_search",
            "args": "{",
            "id": "broken",
            "error": "invalid JSON",
            "type": "invalid_tool_call",
        }
    ]
    with pytest.raises(ValueError, match="invalid tool calls"):
        decision_from_ai_message(response)


@pytest.mark.asyncio
async def test_chat_cleans_up_persistence_failure(monkeypatch):
    runtime, _, _, context = single(monkeypatch)
    context.complete_turn = AsyncMock(side_effect=RuntimeError("scripted persistence failure"))
    runtime._mark_run_failed = AsyncMock()
    request = ChatRequest(message="查询键盘")
    with pytest.raises(RuntimeError, match="scripted persistence failure"):
        await runtime.run(request, 1)
    runtime._mark_run_failed.assert_awaited_once()
