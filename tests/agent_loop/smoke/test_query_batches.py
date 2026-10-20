"""Multi-goal batches traverse the official runtime and tool-free answer stage."""

import json

import pytest
from app.schemas.chat import ChatRequest

from agent_loop.support.domain_tools import make_catalog
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from agent_loop.support.query_batches import with_handlers

pytestmark = [pytest.mark.smoke, pytest.mark.asyncio]


@pytest.mark.parametrize("failure", [False, True])
async def test_independent_goals_need_two_query_requests_and_one_answer(monkeypatch, failure):
    catalog = make_catalog(monkeypatch)

    async def fail(request):
        raise OSError("synthetic unavailable")

    parts = ["查 SKU 9101", "查订单 8201"]
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(parts[0]), goal(parts[1], goal_id="g2"))["calls"],
              ("catalog_search", {"query": parts[0]}, "g1"),
              ("order_lookup", {"order_id": 8201}, "g2")),
        finish(),
        final_answer(),
    ], catalog_factory=lambda _: with_handlers(
        catalog, **({"order_lookup": fail} if failure else {}),
    ))
    response = await runtime.run(ChatRequest(message="；".join(parts)), 41)
    assert response.execution_details.model_calls == {"query": 2, "answer": 1}
    assert len(provider.requests) == 3 and "tools" not in provider.requests[-1]
    assert response.products[0].sku_id == 9101
    assert (response.order is None) == failure
    audit = repo.data["audit"]["agent_loop"]
    assert [b["goal_id"] for b in audit["query_batches"][0]["bindings"]] == ["g1", "g2"]
    assert audit["tool_attempts"] == 2 and audit["stop_reason"] == "completed"


async def test_multiple_non_query_goals_finish_without_business_calls(monkeypatch):
    parts = ["介绍一下你", "不要替我付款"]
    runtime, provider, _ = runtime_fixture(monkeypatch, [
        calls(*register(*(goal(p, goal_id=f"g{i}", disposition="direct_response")
                          for i, p in enumerate(parts)))["calls"],
              ("query_complete", {"completion": {"reason": "No queries needed"}})),
        final_answer(),
    ])
    response = await runtime.run(ChatRequest(message="；".join(parts)), 41)
    assert response.execution_details.model_calls == {"query": 1, "answer": 1}
    assert response.execution_details.tool_calls == []
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert len(facts["goals"]) == 2 and facts["evidence"] == []


async def test_independent_branch_and_dependent_branch_execute_in_distinct_rounds(monkeypatch):
    parts = ["查 SKU 9101", "查订单 8201", "找到商品才列规格"]
    goals = [goal(parts[0]), goal(parts[1], goal_id="g2"),
             goal(parts[2], goal_id="g3", prerequisites=[{
                 "goal_id": "g1", "requirement": "product_found", "source_text": "找到商品",
             }])]
    runtime, _, repo = runtime_fixture(monkeypatch, [
        calls(*register(*goals)["calls"], ("catalog_search", {"query": parts[0]}),
              ("order_lookup", {"order_id": 8201}, "g2")),
        calls(("catalog_facets", {"query": "SKU 9101 有哪些规格"}, "g3")),
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message="；".join(parts)), 41)
    assert response.execution_details.model_calls == {"query": 3, "answer": 1}
    audit = repo.data["audit"]["agent_loop"]
    assert [[b["goal_id"] for b in batch["bindings"]] for batch in audit["query_batches"]] == [
        ["g1", "g2"], ["g3"], [],
    ]
    assert audit["rejections"] == []
