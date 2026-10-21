"""P2 independent business expectations; scripted semantics, real domain services."""

import json

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
from app.schemas.chat import ChatRequest
from app.tools.contracts import BoundTool, ToolCatalog

pytestmark = [pytest.mark.business, pytest.mark.asyncio]


async def test_product_order_policy_goals_keep_separate_evidence(monkeypatch):
    parts = ["查 SKU 9101 的价格", "看看订单 8201", "说明退货配件要求"]
    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            calls(
                *register(*(goal(p, goal_id=f"g{i + 1}") for i, p in enumerate(parts)))["calls"],
                ("catalog_search", {"query": parts[0]}, "g1"),
                ("order_lookup", {"order_id": 8201}, "g2"),
                ("policy_search", {"query": "退货配件要求"}, "g3"),
            ),
            finish(),
            final_answer(),
        ],
    )
    response = await runtime.run(ChatRequest(message="；".join(parts)), 41)
    assert response.products[0].sku_id == 9101 and response.order.id == 8201
    assert response.evidence[0].source_id == 7301
    assert len(response.execution_details.goals) == 3
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [g["source_text"] for g in facts["goals"]] == parts
    assert [r["invocation"]["goal_id"] for r in facts["evidence"]] == ["g1", "g2", "g3"]
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "completed"
    assert response.execution_details.model_calls == {"query": 2, "answer": 1}


async def test_direct_plus_query_keeps_both_goals(monkeypatch):
    parts = ["简单说说你能帮忙做什么", "再查 SKU 9101"]
    runtime, provider, _ = runtime_fixture(
        monkeypatch,
        [
            register(
                goal(parts[0], disposition="direct_response"),
                goal(parts[1], goal_id="g2"),
            ),
            calls(("catalog_search", {"query": "SKU 9101"}, "g2")),
            finish(),
            final_answer(),
        ],
    )
    response = await runtime.run(ChatRequest(message="，".join(parts)), 41)
    assert len(response.execution_details.goals) == 2
    assert response.execution_details.goals[0].task_ids == []
    assert response.products[0].sku_id == 9101
    assert len(json.loads(provider.requests[-1]["messages"][1]["content"])["goals"]) == 2


async def test_unknown_return_eligibility_blocks_order_lookup(monkeypatch):
    first, second = "说明退货配件要求", "符合退货条件才查订单 8201"
    runtime, _, repo = runtime_fixture(
        monkeypatch,
        [
            register(
                goal(first),
                goal(
                    second,
                    goal_id="g2",
                    prerequisites=[
                        {
                            "goal_id": "g1",
                            "requirement": "condition",
                            "source_text": "符合退货条件",
                        }
                    ],
                ),
            ),
            calls(("policy_search", {"query": "退货配件要求"})),
            calls(("order_lookup", {"order_id": 8201}, "g2")),
            finish(),
            final_answer(),
        ],
    )
    response = await runtime.run(ChatRequest(message=first + "，" + second), 41)
    assert response.order is None
    assert [c.tool_name for c in response.execution_details.tool_calls] == ["policy_search"]
    assert repo.data["audit"]["agent_loop"]["goals"][1]["prerequisites_met"] is False


@pytest.mark.parametrize("sku,found", [(9101, True), (9999, False)])
async def test_product_prerequisite_uses_actual_result_and_context(monkeypatch, sku, found):
    first, second = f"查 SKU {sku}", "找到商品再列规格"
    steps = [
        register(
            goal(first),
            goal(
                second,
                goal_id="g2",
                prerequisites=[
                    {
                        "goal_id": "g1",
                        "requirement": "product_found",
                        "source_text": "找到商品",
                    }
                ],
            ),
        ),
        calls(("catalog_search", {"query": first})),
    ]
    steps.append(calls(("catalog_facets", {"query": f"SKU {sku} 有哪些规格"}, "g2")))
    steps += [finish(), final_answer()]
    runtime, _, repo = runtime_fixture(monkeypatch, steps)
    catalog = make_catalog(monkeypatch)
    facet = catalog.get_by_llm_name("catalog_facets")
    seen = []

    async def observe(request):
        seen.append(request)
        return await facet.handler(request)

    runtime.catalog_factory = lambda model: ToolCatalog(
        [
            BoundTool(b.contract, observe if b.contract.llm_name == "catalog_facets" else b.handler)
            for b in catalog.list_bound_tools()
        ]
    )
    response = await runtime.run(ChatRequest(message=first + "，" + second), 41)
    assert len(response.execution_details.tool_calls) == (2 if found else 1)
    assert bool(seen) == found
    if found:
        assert seen[0].catalog_context.dependency_artifacts[0].selected_sku_ids == [9101]
    assert repo.data["audit"]["agent_loop"]["goals"][1]["prerequisites_met"] is found


async def test_partial_failure_keeps_product_and_failed_goal(monkeypatch):
    first, second = "查 SKU 9101", "查接口知识"
    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            calls(
                *register(goal(first), goal(second, goal_id="g2"))["calls"],
                ("catalog_search", {"query": first}, "g1"),
                ("knowledge_search", {"query": "接口知识"}, "g2"),
            ),
            finish(),
            {"content": "bad"},
            {"content": "bad"},
        ],
    )
    catalog = make_catalog(monkeypatch)

    async def fail(request):
        raise RuntimeError("synthetic unavailable")

    runtime.catalog_factory = lambda model: ToolCatalog(
        [
            BoundTool(b.contract, fail if b.contract.llm_name == "knowledge_search" else b.handler)
            for b in catalog.list_bound_tools()
        ]
    )
    response = await runtime.run(ChatRequest(message=first + "，" + second), 41)
    assert response.products[0].sku_id == 9101
    assert "执行失败" in response.answer and "299" in response.answer
    assert repo.data["audit"]["agent_loop"]["goals"][1]["execution"]["error_count"] == 1
    assert len(provider.requests) == 4
