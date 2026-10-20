"""Strict loop shapes, identical single/batch validation, no semantic tool routing."""

import json
from copy import deepcopy

import pytest
from app.agent.query_schema import CompletionCall, RegistrationCall
from app.schemas.chat import ChatRequest
from pydantic import ValidationError

from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from agent_loop.support.query_batches import capture_sessions

pytestmark = pytest.mark.contract
MESSAGE = "查 SKU 9101"
SEARCH = ("catalog_search", {"query": MESSAGE})


@pytest.mark.parametrize("reference", [
    {"kind": "catalog_candidate"},
    {"kind": "order_candidate", "position": None},
    {"kind": "catalog_candidate", "position": "1"},
    {"kind": "catalog_candidate", "position": True},
    {"kind": "catalog_candidate", "position": 1.0},
    {"kind": "catalog_candidate", "position": 0},
    {"kind": "catalog_candidate", "position": 21},
    {"kind": "catalog_candidate", "position": 1, "sku_id": 9101},
    {"kind": "last_order", "position": 1},
    {"kind": "referenced_product", "position": None},
    {"kind": "made_up", "position": 1},
])
def test_reference_structure_is_checked_by_pydantic(reference):
    with pytest.raises(ValidationError):
        RegistrationCall.model_validate({
            "registration": {"goals": [goal(MESSAGE, references=[reference])]},
        })


@pytest.mark.parametrize("references", [
    None, {}, "first two", [None],
    [{"kind": "last_order"}] * 21,
])
def test_reference_collection_has_an_explicit_type_and_bound(references):
    with pytest.raises(ValidationError):
        RegistrationCall.model_validate({
            "registration": {"goals": [goal(MESSAGE, references=references)]},
        })


def test_nested_schema_expresses_required_positions_and_forbids_extra_fields():
    schema = RegistrationCall.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["registration"]
    definitions = schema["$defs"]
    for value in definitions.values():
        if value.get("type") == "object":
            assert value["additionalProperties"] is False
    candidate = definitions["CandidateReference"]
    assert candidate["required"] == ["kind", "position"]
    assert candidate["properties"]["position"] == {
        "maximum": 20, "minimum": 1, "title": "Position", "type": "integer",
    }
    assert "position" not in definitions["SelectedReference"]["properties"]
    assert "reference" not in definitions["QueryGoal"]["properties"]
    assert definitions["QueryGoal"]["properties"]["references"]["items"]["discriminator"][
        "propertyName"
    ] == "kind"
    # Both ends of the declared range are valid, without interpreting any user text.
    RegistrationCall.model_validate({"registration": {"goals": [goal(
        MESSAGE, references=[
            {"kind": "catalog_candidate", "position": 1},
            {"kind": "order_candidate", "position": 20},
            {"kind": "last_order"},
            {"kind": "referenced_product"},
        ],
    )]}})


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("fault", [
    "outer_extra", "missing_wrapper", "inner_extra", "goal_extra", "string_position",
    "bool_position", "missing_position", "selected_position", "legacy_reference",
    "numeric_goal", "object_goals",
])
async def test_registration_uses_same_strict_checks_before_single_or_batched_execution(
    monkeypatch, batched, fault,
):
    sessions = capture_sessions(monkeypatch)
    bad = deepcopy(register(goal(MESSAGE))["calls"][0][1])
    g = bad["registration"]["goals"][0]
    if fault == "outer_extra":
        bad["user_id"] = 99
    elif fault == "missing_wrapper":
        bad = bad["registration"]
    elif fault == "inner_extra":
        bad["registration"]["condition_met"] = True
    elif fault == "goal_extra":
        g["condition_met"] = True
    elif fault in {"string_position", "bool_position", "missing_position"}:
        ref = {"kind": "catalog_candidate"}
        if fault != "missing_position":
            ref["position"] = "1" if fault == "string_position" else True
        g["references"] = [ref]
    elif fault == "selected_position":
        g["references"] = [{"kind": "last_order", "position": 1}]
    elif fault == "legacy_reference":
        g["reference"] = {"kind": "last_order"}
    elif fault == "numeric_goal":
        g["id"] = 1
    else:
        # A JSON object where an array is required; no automatic collection coercion.
        bad["registration"]["goals"] = {"g1": g}

    def recover(payload):
        session = sessions[0]
        assert session.book.goals == {}
        assert session.adapter.evidence.all_records() == []
        assert session.adapter._operations == {}
        assert session.rejections == ["invalid_control_input"]
        return calls(*register(goal(MESSAGE))["calls"], SEARCH)

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(("register_goals", bad), *([SEARCH] if batched else [])),
        recover, finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert [p.sku_id for p in response.products] == [9101]
    assert len(response.execution_details.tool_calls) == 1
    specs = provider.requests[0]["tools"]
    registration = next(t["function"] for t in specs if t["function"]["name"] == "register_goals")
    assert registration["parameters"] == RegistrationCall.model_json_schema()
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("fault", ["outer_extra", "numeric_reason", "bad_evidence", "extra_status"])
async def test_completion_uses_same_strict_checks_and_keeps_one_repair(
    monkeypatch, batched, fault,
):
    message = "介绍一下你；不要替我付款"
    registration = register(goal(message, disposition="direct_response"))
    completion = {"completion": {"reason": "无需业务查询"}}
    bad = deepcopy(completion)
    g = bad["completion"]
    if fault == "outer_extra":
        bad["user_id"] = 99
    elif fault == "numeric_reason":
        g["reason"] = 1
    elif fault == "bad_evidence":
        g["evidence_ids"] = [123]
    else:
        g["status"] = "handled"
    if batched:
        steps = [
            calls(*registration["calls"], ("query_complete", bad)),
            calls(*registration["calls"], ("query_complete", completion)),
        ]
    else:
        steps = [
            registration, calls(("query_complete", bad)),
            calls(("query_complete", completion)),
        ]
    runtime, provider, repo = runtime_fixture(monkeypatch, [*steps, final_answer()])
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.execution_details.tool_calls == []
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "completed"
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["invalid_control_input"]
    spec = next(t["function"] for t in provider.requests[0]["tools"]
                if t["function"]["name"] == "query_complete")
    assert spec["parameters"] == CompletionCall.model_json_schema()


@pytest.mark.asyncio
async def test_program_does_not_infer_tool_choice_or_order_from_user_prose(monkeypatch):
    message = "先查 SKU 9101，然后看看退货要求"
    # The model chooses policy first; both are permitted, independent read-only calls.
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        calls(*register(goal(message))["calls"],
              ("policy_search", {"query": "退货配件要求"}), SEARCH),
        finish(), final_answer(),
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    assert [c.tool_name for c in response.execution_details.tool_calls] == [
        "policy_search", "catalog_search",
    ]
    assert len(provider.requests) == 3
    assert repo.data["audit"]["agent_loop"]["rejections"] == []
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["goals"][0]["prerequisites"] == []
