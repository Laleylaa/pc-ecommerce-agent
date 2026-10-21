"""Shared actual request limits and cancellation/persistence boundaries."""

import asyncio
import json

import httpx
import pytest
from agent_loop.support.domain_tools import make_catalog
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from agent_loop.support.strands_provider import SETTINGS
from app.core.llm_calls import invoke_before
from app.core.loop_budget import LoopBudget, LoopLimit, model_stage
from app.core.loop_models import loop_models
from app.schemas.chat import ChatRequest
from app.tools.contracts import BoundTool, ToolCatalog
from langchain_core.messages import HumanMessage

pytestmark = pytest.mark.contract


MESSAGE = "查 SKU 9101 的价格"
SEARCH = calls(("catalog_search", {"query": "SKU 9101"}))


@pytest.mark.asyncio
async def test_full_67_attempt_budget_and_separate_limits():
    budget = LoopBudget()
    for _ in range(8):
        budget.charge_model()
    with pytest.raises(LoopLimit, match="query_model_budget"):
        budget.charge_model()
    for _ in range(8):
        budget.before_tool(None)
        with budget.tool_scope(None):
            for _ in range(7):
                budget.charge_model()
            with pytest.raises(LoopLimit, match="tool_model_budget|query_shared_budget"):
                budget.charge_model()
    with pytest.raises(LoopLimit, match="tool_attempt_budget"):
        budget.before_tool(None)
    with pytest.raises(LoopLimit, match="query_shared_budget"):
        budget.charge_model()
    budget.begin_answer()
    with model_stage("answer"):
        for _ in range(3):
            budget.charge_model()
        with pytest.raises(LoopLimit, match="total_model_budget"):
            budget.charge_model()
    assert sum(budget.calls.values()) == 67


@pytest.mark.asyncio
async def test_tool_internal_context_required_and_deadline_not_renewable():
    budget = LoopBudget()
    with model_stage("tool_review"), pytest.raises(LoopLimit, match="outside_attempt"):
        budget.charge_model()
    budget.started -= 191
    with pytest.raises(LoopLimit, match="stage_deadline"):
        budget.charge_model()
    budget.begin_answer()
    with model_stage("answer"):
        budget.charge_model()
    budget.closed = True
    with pytest.raises(LoopLimit, match="stage_deadline"):
        budget.before_tool(None)


@pytest.mark.asyncio
async def test_real_http_tool_planning_review_attempts_share_budget_and_close_client():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    budget = LoopBudget()
    async with loop_models(SETTINGS, budget, transport=httpx.MockTransport(respond)) as (
        _,
        model,
        usage,
    ):
        client = model.client
        with budget.tool_scope(None):
            for i in range(7):
                await invoke_before(
                    model,
                    [HumanMessage(content="synthetic")],
                    budget.query_deadline,
                    stage="tool_review" if i % 2 else "tool_planning",
                )
            with pytest.raises(LoopLimit, match="tool_model_budget"):
                await model.ainvoke([HumanMessage(content="denied")])
        assert len(requests) == 7
        assert budget.calls == {"tool_planning": 4, "tool_review": 3}
        assert usage.summary()["http_attempts"] == 7
        assert all(json.loads(r.content).get("tools") is None for r in requests)
    assert client.is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["query", "tool", "answer", "persist"])
async def test_cancel_stops_new_calls_and_rolls_back_without_reply(monkeypatch, phase):
    started = asyncio.Event()
    released = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            released.set()

    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            finish(),
            final_answer(),
        ],
    )
    if phase == "persist":
        monkeypatch.setattr(runtime.context, "complete_turn", hang)
    elif phase == "tool":
        catalog = make_catalog(monkeypatch)
        runtime.catalog_factory = lambda model: ToolCatalog(
            [
                BoundTool(
                    b.contract, hang if b.contract.llm_name == "catalog_search" else b.handler
                )
                for b in catalog.list_bound_tools()
            ]
        )
    else:

        async def send(request):
            payload = json.loads(request.content)
            if (phase == "query" and not provider.requests) or (
                phase == "answer" and "tools" not in payload
            ):
                return await hang()
            return provider(request)

        runtime.transport = httpx.MockTransport(send)
    task = asyncio.create_task(runtime.run(ChatRequest(message=MESSAGE), 41))
    await asyncio.wait_for(started.wait(), 2)
    count = len(provider.requests)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set() and len(provider.requests) == count
    assert [m.role for m in repo.data["messages"]] == ["user"]
    assert repo.data["failures"][-1]["error"]["type"] == "cancelled"


@pytest.mark.asyncio
async def test_query_hanging_tool_deadline_preserves_prior_evidence(monkeypatch):
    message = "查 SKU 9101 的价格和退货政策"
    catalog = make_catalog(monkeypatch)
    released = asyncio.Event()

    async def hang(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            released.set()

    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message)),
            SEARCH,
            calls(("policy_search", {"query": "退货政策"})),
            final_answer(),
        ],
        budget_factory=lambda: LoopBudget(query_seconds=0.2),
    )
    runtime.catalog_factory = lambda model: ToolCatalog(
        [
            BoundTool(b.contract, hang if b.contract.llm_name == "policy_search" else b.handler)
            for b in catalog.list_bound_tools()
        ]
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.products[0].sku_id == 9101 and released.is_set()
    assert len(provider.requests) == 4
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "query_deadline"
    assert repo.data["audit"]["agent_loop"]["goals"][0]["execution"]["error_count"] == 1


@pytest.mark.asyncio
async def test_answer_deadline_falls_back_without_retry_after_expiry(monkeypatch):
    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            finish(),
        ],
        budget_factory=lambda: LoopBudget(answer_seconds=0.02),
    )
    answer_requests = 0
    released = asyncio.Event()

    async def send(request):
        nonlocal answer_requests
        if "tools" in json.loads(request.content):
            return provider(request)
        answer_requests += 1
        try:
            await asyncio.Future()
        finally:
            released.set()

    runtime.transport = httpx.MockTransport(send)
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert released.is_set() and answer_requests == 1
    assert "299" in response.answer
    assert repo.data["audit"]["agent_loop"]["answer_mode"] == "fallback"
    assert repo.data["audit"]["agent_loop"]["usage"]["usage_missing_attempts"] == 1
