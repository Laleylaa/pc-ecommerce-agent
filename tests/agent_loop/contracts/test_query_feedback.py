"""P3 control feedback and phase-specific tool visibility through the real SDK."""

import json

import pytest
from app.schemas.chat import ChatRequest
from app.tools.contracts import default_tool_contracts

from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from agent_loop.support.query_batches import capture_sessions

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


def last_result(payload):
    return json.loads(next(
        m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool"
    ))


@pytest.mark.parametrize(
    "disposition", ["security_refusal", "clarification"],
)
async def test_direct_goal_feedback_reports_only_execution_and_hides_domain_tools(
    monkeypatch, disposition,
):
    message = "说明这部分请求如何处理"

    def complete(payload):
        state = last_result(payload)
        assert "active_goal_id" not in state
        assert state["query_state"] == [{
            "goal_id": "g1", "execution": {
                "attempt_count": 0, "ok_count": 0, "error_count": 0, "active_result_count": 0,
            },
            "prerequisites_met": True,
        }]
        assert {t["function"]["name"] for t in payload["tools"]} == {
            "register_goals", "query_complete",
        }
        return finish()(payload)

    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [register(goal(message, disposition=disposition)), complete, final_answer()],
    )
    result = await runtime.run(ChatRequest(message=message), 41)
    assert {t["function"]["name"] for t in provider.requests[0]["tools"]} == {
        "register_goals", "query_complete",
        *(c.llm_name for c in default_tool_contracts() if c.read_only),
    }
    assert result.execution_details.tool_calls == []
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "completed"
    assert len(provider.requests) == 3


async def test_query_observation_has_current_status_and_unchanged_public_tool_schema(monkeypatch):
    message = "查 SKU 9999"

    def lookup(payload):
        state = last_result(payload)["query_state"][0]
        assert "status" not in state and state["execution"]["attempt_count"] == 0
        specs = {t["function"]["name"]: t["function"] for t in payload["tools"]}
        for contract in default_tool_contracts():
            assert specs[contract.llm_name]["description"] == contract.description
            assert set(specs[contract.llm_name]["parameters"]["properties"]) == {
                "goal_id", "arguments",
            }
            assert set(
                specs[contract.llm_name]["parameters"]["properties"]["arguments"]["properties"]
            ) == set(
                contract.public_input_model.model_fields
            )
        return calls(("catalog_search", {"query": message}))

    def complete(payload):
        state = last_result(payload)
        # No match is a tool result, never a program judgement of user satisfaction.
        assert state["result"]["output"]["status"] == "not_found"
        assert state["query_state"] == [{
            "goal_id": "g1", "execution": {
                "attempt_count": 1, "ok_count": 1, "error_count": 0, "active_result_count": 1,
            },
            "prerequisites_met": True,
        }]
        return finish()(payload)

    runtime, _, _ = runtime_fixture(
        monkeypatch, [register(goal(message)), lookup, complete, final_answer()],
    )
    result = await runtime.run(ChatRequest(message=message), 41)
    assert result.products == []
    assert len(result.execution_details.tool_calls) == 1


async def test_bad_completion_keeps_execution_facts_and_allows_one_repair(monkeypatch):
    message = "查 SKU 9101"
    sessions = capture_sessions(monkeypatch)

    def repair(payload):
        assert sessions[0].rejections == ["invalid_control_input"]
        item = sessions[0].control_state()["query_state"][0]
        assert item["execution"]["ok_count"] == 1
        assert len(sessions[0].book.records("g1")) == 1
        return finish()(payload)

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        register(goal(message)), calls(("catalog_search", {"query": message})),
        calls(("query_complete", {"completion": {"reason": "停止", "evidence_ids": ["invented"]}})),
        repair, final_answer(),
    ])
    result = await runtime.run(ChatRequest(message=message), 41)
    audit = repo.data["audit"]["agent_loop"]
    assert audit["rejections"] == ["invalid_control_input"]
    assert [batch["tools"] for batch in audit["query_batches"]] == [
        ["register_goals"], ["catalog_search"], ["query_complete"], ["query_complete"],
    ]
    assert audit["stop_reason"] == "completed" and len(provider.requests) == 5
    assert result.products[0].sku_id == 9101


async def test_visible_unregistered_query_still_fails_admission(monkeypatch):
    message = "查 SKU 9101"
    runtime, _, repo = runtime_fixture(monkeypatch, [
        calls(("catalog_search", {"query": message})),
        register(goal(message)), calls(("catalog_search", {"query": message})),
        finish(), final_answer(),
    ])
    result = await runtime.run(ChatRequest(message=message), 41)
    assert len(result.execution_details.tool_calls) == 1
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["goal_not_admitted"]
