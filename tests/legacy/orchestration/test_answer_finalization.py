import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.agent.legacy.decisions import FinishAnswerInput
from app.agent.legacy.goal_planning import GoalAnswer
from app.agent.legacy.limits import ANSWER_CALL_POLICY
from app.agent.legacy.runtime import AgentRuntime
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.schemas.context import ContextMessage
from app.tools.schemas import ToolError, ToolExecutionResult
from pydantic import ValidationError

from tests.support.runtime import FakeContext, goal, message, setup_runtime, task
from tests.support.runtime_audit import single


@pytest.mark.parametrize("schema", [GoalAnswer, FinishAnswerInput])
@pytest.mark.parametrize("blank", ["", "  ", "\n\t", "\u2003"])
def test_answer_contract_rejects_blank_text(schema, blank):
    payload = {"response": blank}
    if schema is FinishAnswerInput:
        payload["used_tool_call_ids"] = ["call_1"]
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("declare_coverage", [True, False])
async def test_unavailable_tool_and_non_tool_reply_survive_finalization(
    monkeypatch, declare_coverage
):
    text = "查询键盘；解释刷新率；帮我选办公设备"
    goals = [
        goal("查询键盘"),
        goal("解释刷新率", "goal_2", "direct_response"),
        goal("帮我选办公设备", "goal_3", "clarification"),
    ]
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        text,
        goals,
        [
            {"goal_id": "goal_1", "tasks": [task("查询键盘")]},
        ],
    )
    executor.execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_name="catalog_search",
            ok=False,
            error=ToolError(code="execution_error", message="x"),
        )
    )
    response = "键盘查询暂未完成。刷新率是每秒刷新次数。办公设备更在意什么特点？"
    payload = {"response": response, "unavailable_parts": ["查询键盘", "帮我选办公设备"]}
    if declare_coverage:
        payload["covered_goal_ids"] = ["goal_1", "goal_2", "goal_3"]
    runtime.answer_synthesizer = SimpleNamespace(
        ainvoke=AsyncMock(return_value=message("finish_unavailable", payload)),
    )
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=text), 1)
    )
    if declare_coverage:
        assert state["answer"] == response and state["terminal_guard_status"] == "accepted"
    else:
        assert state["terminal_guard_status"] == "fallback"
        assert all(g["query"] in state["answer"] for g in goals)
        assert "未能生成" in state["answer"]  # Does not replace open knowledge with a greeting.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    [
        "direct_response",
        "session_grounded_response",
        "clarification",
        "security_refusal",
        "human_handoff",
        "out_of_scope",
    ],
)
async def test_summary_repairs_goal_coverage_once_before_persisting(monkeypatch, disposition):
    text = "需要回应的目标"
    runtime, model, executor, context = setup_runtime(
        monkeypatch,
        text,
        [goal(text, disposition=disposition)],
        [],
    )
    if disposition == "session_grounded_response":
        original = context.prepare_turn

        async def prepare(*args):
            prepared = await original(*args)
            prepared.history = [ContextMessage(role="assistant", content="刷新率以Hz为单位。")]
            return prepared

        context.prepare_turn = prepare
    model.scripts["summarize_goals"] = [
        message("summarize_goals", {"response": "遗漏了目标声明"}),
        message(
            "summarize_goals", {"response": "目标已得到明确回应", "covered_goal_ids": ["goal_1"]}
        ),
    ]
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=text), 1)
    )
    assert state["answer"] == "目标已得到明确回应"
    assert state["terminal_guard_status"] == "accepted"
    assert state["answer_call_count"] == state["orchestrator_call_count"] == 2
    assert not executor.calls and context.completed == 1


@pytest.mark.asyncio
async def test_planning_failure_preserves_non_tool_goals_without_calling_llm3(monkeypatch):
    text = "查询键盘；解释刷新率；推荐办公设备"
    goals = [
        goal("查询键盘"),
        goal("解释刷新率", "goal_2", "direct_response"),
        goal("推荐办公设备", "goal_3", "clarification"),
    ]
    runtime, model, executor, context = setup_runtime(monkeypatch, text, goals, [])
    invalid = message("submit_execution_plan", {"tasks": [], "blocked_goals": []})
    model.scripts["submit_execution_plan"] = [invalid, invalid]
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=text), 1)
    )
    assert all(g["query"] in state["answer"] for g in goals)
    assert "规划" in state["answer"] and "特点" in state["answer"]
    assert not model.calls["summarize_goals"] and not executor.calls
    assert state["terminal_guard_status"] == "fallback" and context.completed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [False, True])
async def test_answer_timeout_skips_retry_when_delay_exceeds_remaining_budget(monkeypatch, summary):
    if summary:
        runtime, _, _, context = setup_runtime(
            monkeypatch,
            "解释刷新率",
            [goal("解释刷新率", disposition="direct_response")],
            [],
        )
    else:
        runtime, _, _, context = single(monkeypatch)
    cancelled = asyncio.Event()

    async def hang(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=hang))
    if summary:
        runtime.goal_summarizer = model
    else:
        runtime.answer_synthesizer = model
    runtime.answer_call_policy = replace(
        ANSWER_CALL_POLICY,
        call_timeout_seconds=0.02,
        total_timeout_seconds=0.04,
    )
    state = await asyncio.wait_for(
        runtime._build_graph().ainvoke(
            runtime._initial_state(ChatRequest(message="解释刷新率" if summary else "查询键盘"), 1),
        ),
        1,
    )
    assert cancelled.is_set() and model.ainvoke.await_count == 1
    assert state["answer_call_count"] == state["orchestrator_call_count"] == 1
    assert state["answer"].strip() and context.completed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["load_context", "execute_tool_wave", "persist_turn"])
async def test_whole_turn_deadline_cleans_up_all_stages(monkeypatch, stage):
    runtime, _, executor, context = single(monkeypatch)
    runtime.turn_timeout_seconds = 0.03
    runtime._mark_run_failed = AsyncMock()

    async def hang(*args):
        await asyncio.Event().wait()

    if stage == "load_context":
        context.prepare_turn = hang
    elif stage == "execute_tool_wave":
        executor.execute = hang
    else:
        context.complete_turn = hang
    request = ChatRequest(message="查询键盘")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runtime.run(request, 1), 1)
    runtime._mark_run_failed.assert_awaited_once()
    failed_state = runtime._mark_run_failed.call_args.args[0]
    if stage != "load_context":
        assert failed_state["run_id"] == 1
    if stage == "persist_turn":
        assert failed_state["tool_results"] and failed_state["answer"]


@pytest.mark.asyncio
async def test_external_cancellation_is_propagated_after_cleanup(monkeypatch):
    runtime, _, _, _ = single(monkeypatch)
    started = asyncio.Event()

    async def hang(*args):
        started.set()
        await asyncio.Event().wait()

    runtime.answer_synthesizer = SimpleNamespace(ainvoke=AsyncMock(side_effect=hang))
    runtime._mark_run_failed = AsyncMock()
    request = ChatRequest(message="查询键盘")

    async def consume():
        return await runtime.run(request, 1)

    running = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    runtime._mark_run_failed.assert_awaited_once()
    assert runtime._mark_run_failed.call_args.args[1] == "cancelled"
    assert runtime.answer_synthesizer.ainvoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["audit_error", "audit_hang", "rollback_error"])
async def test_cleanup_failures_preserve_primary_failure_and_are_bounded(monkeypatch, failure):
    runtime, _, _, context = single(monkeypatch)
    order = []

    async def rollback():
        order.append("rollback")
        if failure == "rollback_error":
            raise RuntimeError("rollback failed")

    async def fail(*args):
        order.append("audit")
        if failure == "audit_hang":
            await asyncio.Event().wait()
        raise RuntimeError("audit failed")

    async def complete(*args):
        runtime.session = SimpleNamespace(rollback=rollback)
        raise RuntimeError("original write failure")

    context.complete_turn = complete
    runtime._mark_run_failed = AsyncMock(side_effect=fail)
    runtime.cleanup_timeout_seconds = 0.02
    with pytest.raises(RuntimeError, match="original write failure"):
        await asyncio.wait_for(runtime.run(ChatRequest(message="查询键盘"), 1), 1)
    assert order == (["rollback"] if failure == "rollback_error" else ["rollback", "audit"])


@pytest.mark.asyncio
async def test_runtime_answer_http_client_has_deadline_and_no_hidden_retries():
    runtime = AgentRuntime(
        None, Settings(llm_api_key="test-placeholder"), context_service=FakeContext()
    )
    try:
        assert runtime.llm.request_timeout == 30 and runtime.llm.max_retries == 0
    finally:
        await runtime.llm.root_async_client.close()
        runtime.llm.root_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "network", "service", "auth", "program"])
async def test_answer_transport_retry_policy_on_both_paths(monkeypatch, summary, failure):
    import httpx
    from openai import APIStatusError

    text = "解释刷新率" if summary else "查询键盘"
    if summary:
        runtime, _, _, context = setup_runtime(
            monkeypatch, text, [goal(text, disposition="direct_response")], [],
        )
    else:
        runtime, _, _, context = single(monkeypatch)
    if failure in {"service", "auth"}:
        error = APIStatusError(
            "provider rejected", body=None,
            response=httpx.Response(
                503 if failure == "service" else 401,
                request=httpx.Request("POST", "https://model.invalid"),
            ),
        )
    else:
        error = {"timeout": TimeoutError(), "network": httpx.ConnectError("offline"),
                 "program": RuntimeError("bug")}[failure]
    valid = message(
        "summarize_goals" if summary else "finish_answer",
        {"response": "已根据有效材料完成回答。", "covered_goal_ids": ["goal_1"],
         **({} if summary else {"used_tool_call_ids": ["planned_task_1"]})},
    )
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=[error, valid]))
    setattr(runtime, "goal_summarizer" if summary else "answer_synthesizer", model)
    runtime.answer_call_policy = replace(ANSWER_CALL_POLICY, retry_delay_seconds=0)
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=text), 1)
    )
    retryable = failure in {"timeout", "network", "service"}
    assert model.ainvoke.await_count == (2 if retryable else 1)
    assert state["answer_call_count"] == model.ainvoke.await_count
    assert context.completed == 1
    if retryable:
        assert state["answer"] == "已根据有效材料完成回答。"
        assert model.ainvoke.call_args_list[0] == model.ainvoke.call_args_list[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("sequence", ["transport_repair", "repair_transport", "exhausted"])
async def test_answer_transport_and_repair_have_separate_bounded_allowances(
    monkeypatch, summary, sequence,
):
    from langchain_core.messages import AIMessage

    text = "解释刷新率" if summary else "查询键盘"
    if summary:
        runtime, _, _, context = setup_runtime(
            monkeypatch, text, [goal(text, disposition="direct_response")], [],
        )
    else:
        runtime, _, _, context = single(monkeypatch)
    valid = message(
        "summarize_goals" if summary else "finish_answer",
        {"response": "修复后正常回答。", "covered_goal_ids": ["goal_1"],
         **({} if summary else {"used_tool_call_ids": ["planned_task_1"]})},
    )
    invalid = AIMessage(content="not a function call")
    scripts = {
        "transport_repair": [TimeoutError(), invalid, valid],
        "repair_transport": [invalid, TimeoutError(), valid],
        "exhausted": [TimeoutError(), TimeoutError(), valid],
    }
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=scripts[sequence]))
    setattr(runtime, "goal_summarizer" if summary else "answer_synthesizer", model)
    runtime.answer_call_policy = replace(ANSWER_CALL_POLICY, retry_delay_seconds=0)
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=text), 1)
    )
    assert model.ainvoke.await_count == (2 if sequence == "exhausted" else 3)
    assert state["answer_call_count"] == model.ainvoke.await_count
    assert context.completed == 1
    if sequence != "exhausted":
        assert state["answer"] == "修复后正常回答。"
        calls = model.ainvoke.call_args_list
        if sequence == "repair_transport":
            assert calls[1] == calls[2]
        else:
            assert calls[0] == calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [False, True])
async def test_answer_real_timeout_cancels_attempt_then_retries(monkeypatch, summary):
    text = "解释刷新率" if summary else "查询键盘"
    if summary:
        runtime, _, _, context = setup_runtime(
            monkeypatch, text, [goal(text, disposition="direct_response")], [],
        )
    else:
        runtime, _, _, context = single(monkeypatch)
    cancelled = asyncio.Event()
    attempts = 0

    async def respond(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        assert cancelled.is_set()
        return message(
            "summarize_goals" if summary else "finish_answer",
            {"response": "超时重试后完成。", "covered_goal_ids": ["goal_1"],
             **({} if summary else {"used_tool_call_ids": ["planned_task_1"]})},
        )

    setattr(runtime, "goal_summarizer" if summary else "answer_synthesizer",
            SimpleNamespace(ainvoke=respond))
    runtime.answer_call_policy = replace(
        ANSWER_CALL_POLICY, call_timeout_seconds=0.02,
        total_timeout_seconds=0.5, retry_delay_seconds=0,
    )
    state = await asyncio.wait_for(runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=text), 1),
    ), 2)
    assert attempts == state["answer_call_count"] == 2
    assert state["answer"] == "超时重试后完成。" and context.completed == 1
