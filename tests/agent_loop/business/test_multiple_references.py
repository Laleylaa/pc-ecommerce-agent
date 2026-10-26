"""A comparison can select multiple historical objects without splitting its goal."""

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
from agent_loop.support.query_batches import capture_sessions, with_handlers
from app.schemas.chat import ChatRequest
from app.schemas.context import WorkingMemoryV2
from app.tools.schemas import ToolError, ToolExecutionResult

pytestmark = [pytest.mark.business, pytest.mark.asyncio]


def history():
    return WorkingMemoryV2.model_validate({
        "catalog": {
            "candidate_sku_ids": [9102, 9101, 9999],
            "referenced_sku_id": 9999,
        },
        "order": {"candidate_order_ids": [8201, 9999], "last_order_id": 8201},
    }).model_dump(mode="json")


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("second_fails", [False, True])
async def test_two_product_references_keep_prices_evidence_and_no_single_selection(
    monkeypatch, split, second_fails,
):
    message = "把第一款和第二款的现价分开列出来"
    refs = [{"kind": "catalog_candidate", "position": i} for i in (1, 2)]
    goals = (
        [goal(message, goal_id=f"g{i}", references=[ref]) for i, ref in enumerate(refs, 1)]
        if split else [goal(message, references=refs)]
    )
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(*goals)["calls"],
              ("catalog_search", {"query": "SKU 9102"}, "g1"),
              ("catalog_search", {"query": "SKU 9101"}, "g2" if split else "g1")),
        finish(), final_answer(),
    ], working_memory=history())
    if second_fails:
        catalog = make_catalog(monkeypatch)
        handler = catalog.get_by_llm_name("catalog_search").handler

        async def search(request):
            if "9101" in request.query:
                return ToolExecutionResult(
                    tool_name="catalog_search", ok=False,
                    error=ToolError(code="unavailable", message="synthetic", retryable=False),
                )
            return await handler(request)

        runtime.catalog_factory = lambda model: with_handlers(catalog, catalog_search=search)
    response = await runtime.run(ChatRequest(message=message), 41)
    assert [(p.sku_id, p.price) for p in response.products] == (
        [(9102, 199)] if second_fails else [(9102, 199), (9101, 299)]
    )
    assert len(provider.requests) == 3
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    resolved = [r for g in facts["goals"] for r in g["trusted_references"]]
    assert resolved == [
        {"reference": refs[0], "identity": {"sku_id": 9102}},
        {"reference": refs[1], "identity": {"sku_id": 9101}},
    ]
    assert [r["invocation"]["arguments"]["query"] for r in facts["evidence"]] == [
        "SKU 9102", "SKU 9101",
    ]
    assert len({r["evidence_id"] for r in facts["evidence"]}) == 2
    memory = repo.data["working_memory"]["catalog"]
    assert memory["referenced_sku_id"] is None and memory["referenced_spu_id"] is None
    assert memory["candidate_sku_ids"] == ([9102] if second_fails else [9102, 9101])
    assert repo.data["audit"]["agent_loop"]["rejections"] == []
    if not second_fails:
        # Subsequent positions use the actual displayed order, not the last tool call.
        followup = "第二款的库存再查一下"
        provider.steps.extend([
            calls(*register(goal(followup, references=[refs[1]]))["calls"],
                  ("catalog_search", {"query": "SKU 9101 库存"})),
            finish(), final_answer(),
        ])
        again = await runtime.run(ChatRequest(message=followup, conversation_id=7), 41)
        assert [p.sku_id for p in again.products] == [9101]
        assert repo.data["working_memory"]["catalog"]["referenced_sku_id"] == 9101


@pytest.mark.parametrize("fault", ["out_of_range"])
async def test_multiple_selection_rejects_nonexistent_structured_reference(monkeypatch, fault):
    message = "比较前两款的价格"
    sessions = capture_sessions(monkeypatch)
    refs = [{"kind": "catalog_candidate", "position": i} for i in (1, 2)]
    invalid_refs = [
        refs[0], {"kind": "catalog_candidate", "position": 4},
    ]

    def recover(payload):
        assert sessions[0].book.goals == {}
        assert sessions[0].adapter.evidence.all_records() == []
        assert sessions[0].adapter._operations == {}
        return calls(*register(goal(message, references=refs))["calls"],
                     ("catalog_search", {"query": "SKU 9102"}),
                     ("catalog_search", {"query": "SKU 9101"}))

    runtime, _, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(message, references=invalid_refs))["calls"],
              ("catalog_search", {"query": "SKU 9102"}),
              ("catalog_search", {"query": "SKU 9999"})),
        recover, finish(), final_answer(),
    ], working_memory=history())
    response = await runtime.run(ChatRequest(message=message), 41)
    assert [p.sku_id for p in response.products] == [9102, 9101]
    assert repo.data["audit"]["agent_loop"]["rejections"] == [
        "reference_ambiguous_or_missing",
    ]


async def test_one_goal_can_query_two_historical_orders_without_selecting_the_last(monkeypatch):
    message = "刚才第一笔和第二笔订单分别是什么状态"
    refs = [{"kind": "order_candidate", "position": i} for i in (1, 2)]
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(message, references=refs))["calls"],
              ("order_lookup", {"order_id": 8201}), ("order_lookup", {"order_id": 9999})),
        finish(), final_answer(),
    ], working_memory=history())
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.order is None and [o.id for o in response.orders] == [8201]
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [r["identity"] for r in facts["goals"][0]["trusted_references"]] == [
        {"order_id": 8201}, {"order_id": 9999},
    ]
    assert [r["result"]["output"]["result_type"] for r in facts["evidence"]] == [
        "single_order", "not_found",
    ]
    assert repo.data["working_memory"]["order"]["last_order_id"] is None
