"""First-response batching preserves business goals, references and prerequisites."""

import json

import pytest
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from app.schemas.chat import ChatRequest
from app.schemas.context import WorkingMemoryV2

pytestmark = [pytest.mark.business, pytest.mark.asyncio]


async def test_comparison_first_batch_keeps_found_and_missing_evidence(monkeypatch):
    message = "比较 SKU 9101 和 SKU 9999"
    runtime, provider, _ = runtime_fixture(monkeypatch, [
        calls(*register(goal(message))["calls"],
              ("catalog_search", {"query": "SKU 9101"}),
              ("catalog_search", {"query": "SKU 9999"})),
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    assert len(provider.requests) == 3 and [p.sku_id for p in response.products] == [9101]
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [e["result"]["output"]["status"] for e in facts["evidence"]] == ["success", "not_found"]


async def test_first_batch_resolves_historical_candidate_and_requeries_price(monkeypatch):
    message = "第二款现在多少钱"
    runtime, provider, _ = runtime_fixture(monkeypatch, [
        calls(*register(goal(message, references=[{
            "kind": "catalog_candidate", "position": 2,
        }]))["calls"], ("catalog_search", {"query": "SKU 9102"})),
        finish(), final_answer(),
    ], working_memory=WorkingMemoryV2.model_validate({
        "catalog": {"candidate_sku_ids": [9101, 9102]},
    }).model_dump(mode="json"))
    response = await runtime.run(ChatRequest(message=message), 41)
    assert len(provider.requests) == 3
    assert [(p.sku_id, p.price) for p in response.products] == [(9102, 199)]


async def test_mixed_direct_and_query_goals_are_not_omitted(monkeypatch):
    first, second = "能直接退款吗", "查 SKU 9101"
    runtime, provider, _ = runtime_fixture(monkeypatch, [
        calls(*register(goal(first, disposition="direct_response"),
                        goal(second, goal_id="g2"))["calls"],
              ("catalog_search", {"query": second}, "g2")),
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=first + "，" + second), 41)
    assert len(provider.requests) == 3 and len(response.execution_details.goals) == 2
    assert response.execution_details.goals[0].task_ids == []
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["evidence"][0]["invocation"]["goal_id"] == "g2"


async def test_dependent_goal_cannot_query_until_prior_evidence_exists(monkeypatch):
    first, second = "查 SKU 9101", "找到后才查订单 8201"
    goals = [goal(first), goal(second, goal_id="g2", prerequisites=[{
        "goal_id": "g1", "requirement": "product_found", "source_text": "找到后",
    }])]
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(*goals)["calls"],
              ("catalog_search", {"query": first}, "g1"),
              ("order_lookup", {"order_id": 8201}, "g2")),
        calls(*register(*goals)["calls"], ("catalog_search", {"query": first})),
        calls(("order_lookup", {"order_id": 8201}, "g2")),
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=first + "，" + second), 41)
    assert response.products[0].sku_id == 9101 and response.order.id == 8201
    audit = repo.data["audit"]["agent_loop"]
    assert audit["rejections"] == ["prerequisite_unproven"]
    assert audit["tool_attempts"] == 2 and len(provider.requests) == 5
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [e["invocation"]["goal_id"] for e in facts["evidence"]] == ["g1", "g2"]
