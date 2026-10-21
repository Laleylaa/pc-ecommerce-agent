"""Explicit goal bindings, atomic batch admission and partial-execution boundaries."""

import asyncio
import json

import pytest
from agent_loop.support.domain_tools import TOOL_ARGUMENTS, adapter, make_catalog
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    observations,
    register,
    runtime_fixture,
)
from agent_loop.support.query_batches import capture_sessions, with_handlers
from app.agent.loop.strands_tools import StrandsToolBindings
from app.agent.loop.tool_adapter import AdmissionError
from app.core.loop_budget import LoopBudget
from app.schemas.chat import ChatRequest

pytestmark = pytest.mark.contract
MESSAGE = "查 SKU 9101 的价格，也查订单 8201"
GOALS = [goal("查 SKU 9101 的价格"), goal("查订单 8201", goal_id="g2")]
SEARCH = ("catalog_search", {"query": "SKU 9101"}, "g1")
ORDER = ("order_lookup", {"order_id": 8201}, "g2")


@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
@pytest.mark.parametrize("fault", [
    "missing_goal", "numeric_goal", "empty_goal", "list_goal", "missing_arguments",
    "flat_arguments", "list_arguments", "identity", "evidence", "condition",
])
def test_sdk_envelope_rejects_untrusted_shapes(monkeypatch, name, fault):
    binding = StrandsToolBindings(adapter(make_catalog(monkeypatch)))
    value = {"goal_id": "g1", "arguments": TOOL_ARGUMENTS[name]}
    if fault == "missing_goal":
        value.pop("goal_id")
    elif fault in {"numeric_goal", "empty_goal", "list_goal"}:
        value["goal_id"] = {"numeric_goal": 1, "empty_goal": "", "list_goal": ["g1"]}[fault]
    elif fault == "missing_arguments":
        value.pop("arguments")
    elif fault == "flat_arguments":
        value = {"goal_id": "g1", **TOOL_ARGUMENTS[name]}
    elif fault == "list_arguments":
        value["arguments"] = []
    else:
        value[{"identity": "user_id", "evidence": "evidence_ids",
               "condition": "condition_met"}[fault]] = 99
    with pytest.raises(AdmissionError, match="invalid_input"):
        binding.decode({"name": name, "toolUseId": "x", "input": value})


@pytest.mark.asyncio
async def test_bound_goal_cannot_be_swapped_even_when_both_goals_are_admitted(monkeypatch):
    query = adapter(make_catalog(monkeypatch))
    bindings = StrandsToolBindings(query)
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    bindings.bind("x", operation)
    tool = next(t for t in bindings.tools() if t.tool_name == "order_lookup")
    results = [event async for event in tool.stream({
        "name": "order_lookup", "toolUseId": "x",
        "input": {"goal_id": "g2", "arguments": {"order_id": 8201}},
    }, {})]
    assert results[-1]["tool_result"]["content"][0]["json"]["rejection"] == "bound_goal_mismatch"
    assert query.evidence.all_records() == []


@pytest.mark.asyncio
async def test_invalid_later_batch_preserves_prior_goals_and_evidence(monkeypatch):
    sessions = capture_sessions(monkeypatch)

    def retry(payload):
        session = sessions[0]
        assert set(session.book.goals) == {"g1", "g2"}
        assert len(session.adapter.evidence.all_records()) == 1
        assert len(session.adapter._operations) == len(session._operations) == 1
        assert session.bindings._bindings == {}
        return calls(ORDER)

    runtime, _, repo = runtime_fixture(monkeypatch, [
        calls(*register(*GOALS)["calls"], SEARCH),
        calls(ORDER, ("order_lookup", {"order_id": 8201}, "unknown")),
        retry, finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(response.execution_details.tool_calls) == 2
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["goal_not_admitted"]


@pytest.mark.asyncio
async def test_repeated_sdk_id_rejects_batch_before_any_call(monkeypatch):
    sessions = capture_sessions(monkeypatch)
    initial = calls(*register(*GOALS)["calls"], SEARCH, ORDER)
    initial["tool_use_ids"] = {1: "same", 2: "same"}

    def recover(payload):
        assert sessions[0].book.goals == {}
        assert sessions[0].adapter.evidence.all_records() == []
        return calls(*register(*GOALS)["calls"], SEARCH, ORDER)

    runtime, _, repo = runtime_fixture(monkeypatch, [
        initial, recover, finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(response.execution_details.tool_calls) == 2
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["duplicate_tool_use_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_partial_batch_retry_keeps_success_and_only_replaces_failed_operation(
    monkeypatch, recover,
):
    catalog = make_catalog(monkeypatch)
    original = catalog.get_by_llm_name("order_lookup").handler
    count = 0

    async def flaky(request):
        nonlocal count
        count += 1
        if count == 1 or not recover:
            raise OSError("synthetic dependency outage")
        return await original(request)

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(*GOALS)["calls"], SEARCH, ORDER),
        # Retrying a success together with a failure rejects the entire proposal.
        calls(SEARCH, ORDER), calls(ORDER),
        calls(ORDER),  # Success or the second failure cannot be retried again.
        finish(),
        final_answer(),
    ], catalog_factory=lambda _: with_handlers(catalog, order_lookup=flaky))
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert count == 2 and response.products[0].sku_id == 9101
    search, failed, retried = response.execution_details.tool_calls
    assert search.task_id != failed.task_id == retried.task_id
    assert failed.superseded and not retried.superseded
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert len(facts["evidence"]) == 2
    assert [e["invocation"]["attempt"] for e in facts["evidence"]] == [1, 2]
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["duplicate_query"] * 2


@pytest.mark.asyncio
async def test_completion_cannot_overwrite_program_evidence_ownership(monkeypatch):
    def swapped(payload):
        records = observations(payload)
        return calls(("query_complete", {"completion": {
            "reason": "停止", "goals": [
                {"goal_id": "g1", "evidence_ids": [records[1]["evidence_id"]]},
                {"goal_id": "g2", "evidence_ids": [records[0]["evidence_id"]]},
            ],
        }}))

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(*GOALS)["calls"], SEARCH, ORDER), swapped,
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(response.execution_details.tool_calls) == 2
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["invalid_control_input"]
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [e["invocation"]["goal_id"] for e in facts["evidence"]] == ["g1", "g2"]
    for g in facts["goals"]:
        assert g["evidence_ids"] == [
            e["evidence_id"] for e in facts["evidence"] if e["invocation"]["goal_id"] == g["id"]
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1])
async def test_tool_budget_between_goals_hands_off_only_actual_evidence(monkeypatch, limit):
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(*GOALS)["calls"], SEARCH, ORDER), final_answer(),
    ], budget_factory=lambda: LoopBudget(tool_limit=limit))
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert len(response.execution_details.tool_calls) == limit
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert [g["execution"]["attempt_count"] for g in facts["goals"]] == [limit, 0]
    assert len(facts["evidence"]) == limit
    assert len(provider.requests) == 2
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "tool_attempt_budget"


@pytest.mark.asyncio
async def test_cancel_second_goal_preserves_attempt_audit_without_answer_or_memory(monkeypatch):
    sessions = capture_sessions(monkeypatch)
    entered, released = asyncio.Event(), asyncio.Event()
    catalog = make_catalog(monkeypatch)

    async def hang(request):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            released.set()

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(*GOALS)["calls"], SEARCH, ORDER),
    ], catalog_factory=lambda _: with_handlers(catalog, order_lookup=hang))
    previous = json.dumps(repo.data["working_memory"], sort_keys=True)
    task = asyncio.create_task(runtime.run(ChatRequest(message=MESSAGE), 41))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set() and len(provider.requests) == 1
    records = sessions[0].adapter.evidence.all_records()
    assert records[0].result.ok and records[1].result.error.code == "cancelled"
    assert json.dumps(repo.data["working_memory"], sort_keys=True) == previous
    assert [m.role for m in repo.data["messages"]] == ["user"]
