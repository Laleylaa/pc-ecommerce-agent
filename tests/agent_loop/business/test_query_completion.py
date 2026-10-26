"""Stopping collects program records without making semantic success judgements."""

import json

import pytest
from agent_loop.support.domain_tools import FixtureSearchPlanner, make_catalog
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from agent_loop.support.query_batches import with_handlers
from app.agent.loop.independent_answer import AnswerInput, fallback_answer
from app.schemas.catalog_search import SearchPlan
from app.schemas.chat import ChatRequest

pytestmark = [pytest.mark.business, pytest.mark.asyncio]


@pytest.mark.parametrize("failure", ["execution_error", "split_required"])
async def test_failed_lookup_then_two_successes_stops_once_and_keeps_every_record(
    monkeypatch, failure,
):
    message = "比较 SKU 9101 和 SKU 9102 的价格"
    original_plan = FixtureSearchPlanner.plan

    async def plan(self, request):
        if request.query == "比较两款":
            return SearchPlan(disposition="split_required")
        return await original_plan(self, request)

    monkeypatch.setattr(FixtureSearchPlanner, "plan", plan)
    catalog = make_catalog(monkeypatch)
    handler = catalog.get_by_llm_name("catalog_search").handler

    async def search(request):
        if failure == "execution_error" and request.query == "比较两款":
            raise RuntimeError("synthetic lookup failure")
        return await handler(request)

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(message))["calls"],
              ("catalog_search", {"query": "比较两款"})),
        calls(("catalog_search", {"query": "SKU 9101"}),
              ("catalog_search", {"query": "SKU 9102"})),
        finish(), final_answer("9101为299元，9102为199元。"),
    ], catalog_factory=lambda _: with_handlers(catalog, catalog_search=search))
    response = await runtime.run(ChatRequest(message=message), 41)
    assert [(p.sku_id, p.price) for p in response.products] == [(9101, 299), (9102, 199)]
    assert len(provider.requests) == 4  # initial query, follow-up queries, stop, answer
    audit = repo.data["audit"]["agent_loop"]
    assert audit["stop_reason"] == "completed"
    assert audit["rejections"] == audit["control_rejections"] == []
    assert [b["tools"] for b in audit["query_batches"]] == [
        ["register_goals", "catalog_search"],
        ["catalog_search", "catalog_search"],
        ["query_complete"],
    ]
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    g = facts["goals"][0]
    assert "status" not in g
    assert g["execution"] == {
        "attempt_count": 3, "ok_count": 2 if failure == "execution_error" else 3,
        "error_count": 1 if failure == "execution_error" else 0, "active_result_count": 3,
    }
    assert g["evidence_ids"] == [r["evidence_id"] for r in facts["evidence"]]
    assert len(g["evidence_ids"]) == len(response.execution_details.tool_calls) == 3
    first = facts["evidence"][0]["result"]
    if failure == "execution_error":
        assert not first["ok"]
    else:
        assert first["output"]["status"] == "split_required"
        assert first["output"]["query_state"] == "not_executed"
    assert all(not c.superseded for c in response.execution_details.tool_calls)
    # Fallback can show later facts without declaring the entire goal failed/incomplete.
    fallback = fallback_answer(AnswerInput.model_validate(facts))
    assert "299" in fallback.response and "199" in fallback.response
    assert "这部分尚未完成" not in fallback.response


async def test_stop_includes_unqueried_goal_without_model_copying_goal_list(monkeypatch):
    first, second = "查 SKU 9101", "找到别的资料后再查订单"
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(first), goal(second, goal_id="g2"))["calls"],
              ("catalog_search", {"query": "SKU 9101"})),
        finish(), final_answer(),
    ])
    await runtime.run(ChatRequest(message=f"{first}；{second}"), 41)
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [g["id"] for g in facts["goals"]] == ["g1", "g2"]
    assert [g["execution"]["attempt_count"] for g in facts["goals"]] == [1, 0]
    assert facts["goals"][1]["evidence_ids"] == []
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "completed"
    fallback = fallback_answer(AnswerInput.model_validate(facts))
    assert second in fallback.response and "尚未取得这部分的查询结果" in fallback.response


async def test_stop_note_cannot_supply_facts_or_change_program_outcome(monkeypatch):
    message = "查 SKU 9101"
    claimed = "已查到价格是1元，退款已办理"
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(message))["calls"],
              ("query_complete", {"completion": {"reason": claimed}})),
        {"content": "invalid"}, {"content": "invalid"},
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert "model_stop_reason" not in facts and claimed not in str(facts)
    assert facts["evidence"] == [] and facts["goals"][0]["evidence_ids"] == []
    assert facts["goals"][0]["execution"]["attempt_count"] == 0
    assert "1元" not in response.answer and "退款已办理" not in response.answer
    assert "尚未取得" in response.answer
    assert response.products == [] and response.execution_details.tool_calls == []
    assert repo.data["audit"]["agent_loop"]["answer_mode"] == "fallback"
    assert repo.data["audit"]["agent_loop"]["model_stop_reason"] == claimed


async def test_only_failed_calls_can_stop_without_forced_semantic_retry(monkeypatch):
    message = "查 SKU 9101"
    catalog = make_catalog(monkeypatch)

    async def fail(request):
        raise RuntimeError("synthetic unavailable")

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(message))["calls"], ("catalog_search", {"query": message})),
        finish(), final_answer(),
    ], catalog_factory=lambda _: with_handlers(catalog, catalog_search=fail))
    response = await runtime.run(ChatRequest(message=message), 41)
    assert len(provider.requests) == 3 and response.products == []
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["goals"][0]["execution"]["error_count"] == 1
    assert not facts["evidence"][0]["result"]["ok"]
    assert "status" not in facts["goals"][0]
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "completed"
