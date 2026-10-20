"""R02 P2 complete runtime over real SDK, HTTP mock and formal context service."""

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


async def test_query_handoff_independent_answer_and_commit(monkeypatch):
    message = "查 SKU 9101 的价格"
    runtime, provider, repository = runtime_fixture(
        monkeypatch,
        [
            register(goal(message)),
            calls(("catalog_search", {"query": message})),
            finish(),
            final_answer("该版本价格为299元。"),
        ],
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.answer == "该版本价格为299元。"
    assert response.products[0].sku_id == 9101
    assert len(provider.requests) == 4
    assert all("tools" in p for p in provider.requests[:3])
    assert "tools" not in provider.requests[-1]
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["goals"][0]["execution"]["ok_count"] == 1
    assert len(facts["evidence"]) == 1
    assert not any(m["role"] in {"assistant", "tool"} for m in provider.requests[-1]["messages"])
    assert repository.session.commits == 2
    assert repository.data["messages"][-1].metadata["products"][0]["sku_id"] == 9101
    assert repository.data["working_memory"]["catalog"]["candidate_sku_ids"] == [9101]
    audit = repository.data["audit"]["agent_loop"]
    assert audit["stop_reason"] == "completed"
    assert audit["usage"]["http_attempts"] == 4
    assert response.execution_details.model_calls == {"query": 3, "answer": 1}


async def test_direct_goal_also_uses_separate_answer_without_business_tools(monkeypatch):
    message = "请简要介绍你能够提供哪些帮助"
    runtime, provider, repository = runtime_fixture(
        monkeypatch,
        [
            register(goal(message, disposition="direct_response")),
            finish(),
            final_answer("我能查询商品、本人订单和政策。"),
        ],
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert not response.execution_details.tool_calls
    assert repository.data["audit"]["agent_loop"]["tool_attempts"] == 0
    assert len(provider.requests) == 3
