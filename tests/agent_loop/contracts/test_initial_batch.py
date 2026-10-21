"""Atomic initial admission, rollback, completion guards and cancellation."""

import asyncio
import json

import httpx
import pytest
from agent_loop.support.domain_tools import adapter, make_catalog
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from app.agent.loop import runtime as runtime_module
from app.agent.loop.tool_adapter import AdmissionError
from app.core.loop_budget import LoopBudget
from app.schemas.chat import ChatRequest
from app.tools.contracts import BoundTool, ToolCatalog

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]
MESSAGE = "查 SKU 9101 的价格"
SEARCH = ("catalog_search", {"query": MESSAGE})


def capture_session(monkeypatch):
    sessions = []
    original = runtime_module.QuerySession

    def create(*args):
        session = original(*args)
        sessions.append(session)
        return session

    monkeypatch.setattr(runtime_module, "QuerySession", create)
    return sessions


@pytest.mark.parametrize("bad,code", [
    (("catalog_search", {"query": MESSAGE, "user_id": 99}), "invalid_input"),
    (("catalog_search", {"query": MESSAGE, "limit": "2"}), "invalid_input"),
    (("catalog_search", {"query": "SKU 1234"}), "unsourced_product_identity"),
    (("order_lookup", {"order_id": 1234}), "unsourced_order_identity"),
    (("made_up_tool", {}), "unknown_or_non_query_tool"),
    (SEARCH, "duplicate_pending_query"),
])
async def test_bad_second_call_rolls_back_whole_initial_batch(monkeypatch, bad, code):
    sessions = capture_session(monkeypatch)

    def retry(payload):
        session = sessions[0]
        assert session.book.goals == {}
        assert session.adapter.evidence.all_records() == []
        assert session.adapter._operations == {} and session.adapter._contexts == {}
        assert session._operations == {} and session.bindings._bindings == {}
        assert session.bindings._seen == set()
        assert code in str(payload["messages"])
        return calls(*register(goal(MESSAGE))["calls"], SEARCH)

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(MESSAGE))["calls"], SEARCH, bad),
        retry, finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(response.execution_details.tool_calls) == 1 and len(provider.requests) == 4
    assert repo.data["audit"]["agent_loop"]["rejections"] == [code]


@pytest.mark.parametrize("fault,code", [
    ("source", "goal_source_not_in_current_request"),
    ("extra_registration", "invalid_control_input"),
    ("extra_goal", "invalid_control_input"),
    ("unknown_goal", "goal_not_admitted"),
    ("switch", "unknown_or_non_query_tool"),
    ("query_and_finish", "control_batch_must_be_exclusive"),
])
async def test_initial_controls_fail_closed_without_partial_registration(monkeypatch, fault, code):
    sessions = capture_session(monkeypatch)
    registration = register(goal(MESSAGE))["calls"][0]
    extras = [SEARCH]
    if fault == "source":
        registration[1]["registration"]["goals"][0]["source_text"] = "用户未说的话"
    elif fault == "extra_registration":
        registration[1]["user_id"] = 99
    elif fault == "extra_goal":
        registration[1]["registration"]["goals"][0]["condition_met"] = True
    elif fault == "unknown_goal":
        extras = [("catalog_search", {"query": MESSAGE}, "missing")]
    elif fault == "switch":
        extras = [("activate_goal", {"goal_id": "g1"}), SEARCH]
    else:
        extras += [("query_complete", {"completion": {"goals": []}})]

    def recover(payload):
        assert sessions[0].book.goals == {}
        assert sessions[0].adapter.evidence.all_records() == []
        return calls(*register(goal(MESSAGE))["calls"], SEARCH)

    runtime, _, repo = runtime_fixture(monkeypatch, [
        calls(registration, *extras), recover, finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(response.execution_details.tool_calls) == 1
    assert repo.data["audit"]["agent_loop"]["rejections"] == [code]


async def test_initial_stop_does_not_invent_results_for_unqueried_goal(monkeypatch):
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(MESSAGE))["calls"],
              ("query_complete", {"completion": {"reason": "本轮不再查询"}})),
        final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert response.products == [] and response.execution_details.tool_calls == []
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["goals"][0]["execution"]["attempt_count"] == 0
    assert facts["evidence"] == [] and "status" not in facts["goals"][0]
    assert repo.data["audit"]["agent_loop"]["rejections"] == []


@pytest.mark.parametrize("repair", [True, False])
@pytest.mark.parametrize("fault", ["numeric_reason", "evidence", "blank_reason"])
async def test_non_query_completion_still_allows_only_one_repair(monkeypatch, repair, fault):
    message = "这是什么"
    registration = register(goal(message, disposition="clarification"))["calls"]
    invalid = {"reason": 123}
    if fault == "evidence":
        invalid = {"reason": "停止", "evidence_ids": ["invented"]}
    elif fault == "blank_reason":
        invalid = {"reason": " "}
    bad = calls(*registration, ("query_complete", {"completion": invalid}))
    good = calls(*registration, ("query_complete", {"completion": {"reason": "缺少指代"}}))
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        bad, good if repair else bad, final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    audit = repo.data["audit"]["agent_loop"]
    assert audit["stop_reason"] == ("completed" if repair else "invalid_completion")
    assert len(provider.requests) == 3 and response.execution_details.tool_calls == []
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["evidence"] == [] and facts["goals"][0]["execution"]["attempt_count"] == 0


async def test_duplicate_control_and_domain_ids_reject_whole_first_batch(monkeypatch):
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(MESSAGE))["calls"], SEARCH),
        calls(*register(goal(MESSAGE))["calls"], SEARCH),
        finish(), final_answer(),
    ])

    def send(request):
        response = provider(request)
        if len(provider.requests) == 1:
            body = response.json()
            uses = body["choices"][0]["message"]["tool_calls"]
            uses[1]["id"] = uses[0]["id"]
            return httpx.Response(200, json=body)
        return response

    runtime.transport = httpx.MockTransport(send)
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(response.execution_details.tool_calls) == 1
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["duplicate_tool_use_id"]


async def test_preparation_rollback_preserves_prior_attempts_and_evidence(monkeypatch):
    query = adapter(make_catalog(monkeypatch))
    first = query.prepare("g1", "order_lookup", {"order_id": 8201})
    record = await query.execute(first)
    with pytest.raises(AdmissionError, match="invalid_input"), query.preparing_batch():
        pending = query.prepare("g1", "catalog_search", {"query": "SKU 9101"})
        query.prepare("g1", "order_lookup", {"order_id": 8201, "user_id": 99})
    with pytest.raises(AdmissionError, match="unknown_operation"):
        query.operation(pending)
    with pytest.raises(AdmissionError, match="cannot_discard_attempted_operation"):
        query.discard_prepared(first)
    assert query.attempts(first) == (record,)
    assert query.evidence.all_records() == [record]


async def test_later_registration_cannot_mix_with_query(monkeypatch):
    runtime, _, repo = runtime_fixture(monkeypatch, [
        register(goal(MESSAGE)), calls(*register(goal(MESSAGE))["calls"], SEARCH),
        calls(SEARCH), finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(response.execution_details.tool_calls) == 1
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["control_batch_must_be_exclusive"]


async def test_combined_query_still_uses_shared_budget_and_hands_off_partial(monkeypatch):
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(MESSAGE))["calls"], SEARCH), final_answer(),
    ], budget_factory=lambda: LoopBudget(query_limit=1))
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(provider.requests) == 2 and response.products[0].sku_id == 9101
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "query_model_budget"


async def test_cancel_first_batched_query_does_not_answer_or_persist_success(monkeypatch):
    started, released = asyncio.Event(), asyncio.Event()

    async def hang(request):
        started.set()
        try:
            await asyncio.Future()
        finally:
            released.set()

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(MESSAGE))["calls"], SEARCH),
    ])
    catalog = make_catalog(monkeypatch)
    runtime.catalog_factory = lambda model: ToolCatalog([
        BoundTool(b.contract, hang if b.contract.llm_name == "catalog_search" else b.handler)
        for b in catalog.list_bound_tools()
    ])
    task = asyncio.create_task(runtime.run(ChatRequest(message=MESSAGE), 41))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set() and len(provider.requests) == 1
    assert [m.role for m in repo.data["messages"]] == ["user"]
    assert repo.data["failures"][-1]["error"]["type"] == "cancelled"
