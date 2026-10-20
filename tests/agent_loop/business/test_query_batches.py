"""Query attribution survives cards, persistence and subsequent references."""

import json

import pytest
from app.schemas.chat import ChatRequest
from app.schemas.context import WorkingMemoryV2

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

pytestmark = [pytest.mark.business, pytest.mark.asyncio]


@pytest.mark.parametrize("same_goal", [False, True])
async def test_multiple_catalog_calls_keep_evidence_without_last_filter_leaking(
    monkeypatch, same_goal,
):
    parts = ["查 SKU 9101", "查 SKU 9102"]
    message = "；".join(parts)
    goals = [goal(message)] if same_goal else [
        goal(parts[0]), goal(parts[1], goal_id="g2"),
    ]
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(*goals)["calls"],
              ("catalog_search", {"query": parts[0]}, "g1"),
              ("catalog_search", {"query": parts[1]}, "g1" if same_goal else "g2")),
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    assert [(p.sku_id, p.price) for p in response.products] == [(9101, 299), (9102, 199)]
    records = json.loads(provider.requests[-1]["messages"][1]["content"])["evidence"]
    assert len({r["invocation"]["operation_id"] for r in records}) == 2
    assert len({r["evidence_id"] for r in records}) == 2
    assert [r["invocation"]["goal_id"] for r in records] == ["g1", "g1" if same_goal else "g2"]
    memory = repo.data["working_memory"]["catalog"]
    assert memory["candidate_sku_ids"] == [9101, 9102] and memory["query_plan"] == {}
    queries = repo.data["audit"]["agent_loop"]["query_results"]["catalog"]
    assert len(queries) == 2 and queries[0]["operation_id"] != queries[1]["operation_id"]
    saved = repo.data["messages"][-1].metadata
    assert len(saved["execution_details"]["tool_calls"]) == 2

    # A real second turn must resolve the combined display and query dynamic price again.
    followup = "第二款现在的价格"
    provider.steps.extend([
        calls(*register(goal(followup, references=[{
            "kind": "catalog_candidate", "position": 2,
        }]))["calls"], ("catalog_search", {"query": "SKU 9102"})),
        finish(), final_answer(),
    ])
    second = await runtime.run(ChatRequest(message=followup, conversation_id=7), 41)
    assert [(p.sku_id, p.price) for p in second.products] == [(9102, 199)]
    assert second.execution_details.tool_calls[0].arguments["query"] == "SKU 9102"
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["goals"][0]["trusted_references"] == [{
        "reference": {"kind": "catalog_candidate", "position": 2},
        "identity": {"sku_id": 9102},
    }]
    assert facts["history"] == []


async def test_same_tool_same_arguments_across_goals_remain_distinct(monkeypatch):
    message = "查订单 8201 状态，同时看看订单 8201 物流"
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal("查订单 8201 状态"),
                        goal("看看订单 8201 物流", goal_id="g2"))["calls"],
              ("order_lookup", {"order_id": 8201}, "g1"),
              ("order_lookup", {"order_id": 8201}, "g2")),
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    assert len(response.orders) == 1 and response.orders[0].id == 8201
    assert response.order is None and response.order_query is None
    records = json.loads(provider.requests[-1]["messages"][1]["content"])["evidence"]
    assert len(records) == 2
    assert records[0]["invocation"]["operation_id"] != records[1]["invocation"]["operation_id"]
    memory = repo.data["working_memory"]["order"]
    assert memory["candidate_order_ids"] == [8201]
    assert memory["last_order_id"] is None and memory["is_exhaustive"] is False
    assert len(repo.data["audit"]["agent_loop"]["query_results"]["orders"]) == 2


async def test_multi_order_found_and_missing_does_not_reuse_previous_order_scope(monkeypatch):
    parts = ["查订单 8201", "查订单 9999"]
    previous = WorkingMemoryV2.model_validate({
        "order": {"candidate_order_ids": [8888], "last_order_id": 8888,
                  "total_match_count": 99, "returned_count": 1, "is_exhaustive": True},
    })
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(parts[0]), goal(parts[1], goal_id="g2"))["calls"],
              ("order_lookup", {"order_id": 8201}, "g1"),
              ("order_lookup", {"order_id": 9999}, "g2")),
        finish(), final_answer(),
    ], working_memory=previous.model_dump(mode="json"))
    response = await runtime.run(ChatRequest(message="；".join(parts)), 41)
    assert response.order is None and response.order_query is None
    assert [o.id for o in response.orders] == [8201]
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [e["result"]["output"]["result_type"] for e in facts["evidence"]] == [
        "single_order", "not_found",
    ]
    memory = repo.data["working_memory"]["order"]
    assert memory["candidate_order_ids"] == [8201] and memory["last_order_id"] is None
    assert memory["is_exhaustive"] is False and memory["pending_suggestions"] == []
    assert repo.data["messages"][-1].metadata["order_query"] is None


async def test_same_sku_different_queries_dedup_card_but_keep_both_query_scopes(monkeypatch):
    parts = ["查 SKU 9101 的价格", "查 SKU 9101 的库存"]
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(parts[0]), goal(parts[1], goal_id="g2"))["calls"],
              ("catalog_search", {"query": parts[0]}, "g1"),
              ("catalog_search", {"query": parts[1]}, "g2")),
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message="；".join(parts)), 41)
    assert len(response.products) == 1 and response.products[0].sku_id == 9101
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [r["invocation"]["arguments"]["query"] for r in facts["evidence"]] == parts
    assert len(repo.data["audit"]["agent_loop"]["query_results"]["catalog"]) == 2
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "completed"
    assert len(provider.requests) == 3


@pytest.mark.parametrize("upstream", ["missing", "failed"])
async def test_unproven_dependency_blocks_only_its_branch(monkeypatch, upstream):
    parts = ["查 SKU 9999", "找到后查订单 8201", "说明退货配件要求"]
    catalog = make_catalog(monkeypatch)

    async def fail(request):
        raise OSError("synthetic catalog outage")

    goals = [goal(parts[0]), goal(parts[1], goal_id="g2", prerequisites=[{
        "goal_id": "g1", "requirement": "product_found", "source_text": "找到后",
    }]), goal(parts[2], goal_id="g3")]
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(*goals)["calls"], ("catalog_search", {"query": parts[0]}),
              ("policy_search", {"query": "退货配件要求"}, "g3")),
        calls(("order_lookup", {"order_id": 8201}, "g2")),
        finish(), final_answer(),
    ], catalog_factory=lambda _: with_handlers(
        catalog, **({"catalog_search": fail} if upstream == "failed" else {}),
    ))
    response = await runtime.run(ChatRequest(message="；".join(parts)), 41)
    assert response.order is None and response.evidence[0].source_id == 7301
    assert [c.tool_name for c in response.execution_details.tool_calls] == [
        "catalog_search", "policy_search",
    ]
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["goals"][1]["evidence_ids"] == []
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["prerequisite_unproven"]
