"""Control budget and lossless bounded observations, without live model semantics."""

import json
from datetime import UTC, datetime

import pytest
from app.agent.loop.control_tools import bounded_control_result
from app.agent.loop.evidence import EvidenceStore, ToolInvocation
from app.agent.loop.query_loop import project_record
from app.tools.schemas import ToolExecutionResult

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("accepted", [True, False])
def test_oversize_control_preserves_accepted_state_and_marks_missing_references(
    accepted,
):
    original = {"accepted": accepted, "trusted_references": {"g1": "x" * 30000}}
    result = bounded_control_result(original)
    assert result["accepted"] is accepted and result["output_budget_exceeded"]
    assert "trusted_references" not in result
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 12000
    assert original["trusted_references"]["g1"] == "x" * 30000


def test_small_control_result_is_unchanged():
    original = {"accepted": True, "trusted_references": {"g1": {"sku_id": 1}}}
    assert bounded_control_result(original) == original


@pytest.mark.parametrize("destination", ["query", "answer"])
def test_oversize_invocation_does_not_remove_valid_tool_facts(destination):
    now = datetime.now(UTC)
    result = ToolExecutionResult(
        tool_name="catalog_lookup",
        ok=True,
        output={
            "status": "success",
            "products": [{"sku_id": 1, "price": "100"}],
            "lookup_summary": {"exists": True, "matched_sku_count": 1},
            "output_budget": {"truncated": True, "omitted_sections": ["products"]},
        },
    )
    record = EvidenceStore().record(
        ToolInvocation(
            invocation_id="i1",
            operation_id="op1",
            goal_id="g1",
            tool_name="catalog_lookup",
            arguments={"query": "原始请求" * 10000},
            attempt=1,
            started_at=now,
            elapsed_seconds=1,
            status="succeeded",
        ),
        result,
        now,
    )
    view = project_record(record, destination=destination)
    assert view["result"] == result.model_dump(mode="json")
    assert (
        view["invocation_arguments_omitted"] and view["invocation"]["arguments"] == {}
    )
    assert "tool_output_limited" in view["completion_blockers"]
    assert len(json.dumps(view, ensure_ascii=False)) <= 16000
