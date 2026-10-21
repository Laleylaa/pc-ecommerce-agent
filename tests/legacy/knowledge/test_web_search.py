import asyncio

import httpx
import pytest
from app.schemas.chat import ChatRequest
from app.tools.contracts import (
    BoundTool,
    execute_bound_tool,
)

from tests.support.knowledge_web_search import (
    HIT,
    service,
)
from tests.support.runtime import goal, setup_runtime, task


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["web", "empty", "unconfigured", "timeout", "policy"])
async def test_formal_input_planning_to_tool_output(monkeypatch, case):
    from evals.support.goal_tool_fixtures import knowledge_service

    query = "键盘拆封后还能退吗？" if case == "policy" else "磁轴和机械轴有什么区别？"
    tool = "policy_search" if case == "policy" else "knowledge_search"
    runtime, model, executor, context = setup_runtime(
        monkeypatch, query, [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query, tool=tool)]}],
    )
    http_calls, tool_calls = [], []

    async def respond(request):
        http_calls.append(request)
        if case == "timeout":
            await asyncio.sleep(1)
        return httpx.Response(200, json={"results": [] if case == "empty" else [HIT]})

    monkeypatch.setattr("app.tools.web_search.WEB_SEARCH_TIMEOUT_SECONDS", 0.02)
    handler = (
        knowledge_service().search_policy if case == "policy"
        else service(respond, key="" if case == "unconfigured" else "test-key").search
    )

    async def bound_execute(contract, arguments, runtime_context):
        tool_calls.append(arguments)
        return await execute_bound_tool(BoundTool(contract, handler), arguments, runtime_context)

    executor.execute = bound_execute
    state = runtime._initial_state(ChatRequest(message=query), 1)
    stream = runtime._build_graph().astream(state, stream_mode="updates")
    try:
        async for update in stream:
            for name, values in update.items():
                state.update(values)
                if name == "execute_tool_wave":
                    break
            else:
                continue
            break
    finally:
        await stream.aclose()
    assert len(model.calls["route_request"]) == len(model.calls["submit_execution_plan"]) == 1
    assert not model.calls["summarize_goals"] and context.completed == 0
    assert tool_calls == [{"query": query}]
    result = state["tool_results"][-1]["execution"]
    if case in {"unconfigured", "timeout"}:
        assert not result["ok"] and result["error"]["code"] == (
            "web_search_not_configured" if case == "unconfigured" else "web_search_timeout"
        )
    else:
        assert result["ok"]
        assert result["output"]["result_type"] == ("empty" if case == "empty" else "documents")
        if case == "web":
            assert result["output"]["documents"][0]["url"] == HIT["url"]
            assert state["evidence"][0].source_type == "web_page"
        elif case == "policy":
            assert result["output"]["documents"][0]["source_id"] == 8001
            assert result["output"]["search_strategy"] == "bm25"
    assert len(http_calls) == (0 if case in {"unconfigured", "policy"} else 1)
