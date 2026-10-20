"""P3 first-response batching through the real SDK and independent answer."""

import json

import pytest
from app.schemas.chat import ChatRequest

from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)

pytestmark = [pytest.mark.smoke, pytest.mark.asyncio]


async def test_registration_and_search_share_one_model_response(monkeypatch):
    message = "查 SKU 9101 的价格"
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(message))["calls"], ("catalog_search", {"query": message})),
        finish(),
        final_answer("此版本299元。"),
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.products[0].sku_id == 9101 and response.products[0].price == 299
    assert len(provider.requests) == 3
    assert response.execution_details.model_calls == {"query": 2, "answer": 1}
    assert "tools" not in provider.requests[-1]
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert len(facts["evidence"]) == 1
    assert facts["evidence"][0]["invocation"]["goal_id"] == "g1"
    audit = repo.data["audit"]["agent_loop"]
    assert audit["stop_reason"] == "completed" and audit["tool_attempts"] == 1
    assert audit["query_batches"][0]["tools"] == ["register_goals", "catalog_search"]
    assert repo.data["messages"][-1].metadata["products"][0]["sku_id"] == 9101


async def test_non_query_registration_and_completion_still_use_independent_answer(monkeypatch):
    message = "请简要介绍你能够提供哪些帮助"
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(
            *register(goal(message, disposition="direct_response"))["calls"],
            ("query_complete", {"completion": {"reason": "No queries needed"}}),
        ),
        final_answer("可以查询商品和本人订单，无法办理退款。"),
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    assert len(provider.requests) == 2 and "tools" not in provider.requests[-1]
    assert response.execution_details.model_calls == {"query": 1, "answer": 1}
    assert response.execution_details.tool_calls == []
    audit = repo.data["audit"]["agent_loop"]
    assert audit["stop_reason"] == "completed" and audit["tool_attempts"] == 0
