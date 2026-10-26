"""R09 evidence references, completeness and detached presentation candidates."""

import pytest
from agent_loop.support.domain_tools import adapter, make_catalog
from app.agent.loop.evidence import EvidenceError
from app.agent.loop.tool_adapter import AdmissionError, QueryToolAdapter
from app.tools.contracts import (
    BoundTool,
    ToolCatalog,
    ToolRuntimeContext,
    default_tool_contracts,
)
from app.tools.schemas import OrderLookupOutput, ToolDiagnostic

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def order_record(query, goal="g1", order_id=8201):
    return await query.execute(query.prepare(goal, "order_lookup", {"order_id": order_id}))


async def test_nonexistent_wrong_goal_cross_request_and_superseded_references(monkeypatch):
    catalog = make_catalog(monkeypatch)
    query = adapter(catalog)
    old, new = await order_record(query), await order_record(query)
    for goal, evidence_id, reason in [
        ("g1", "ev_invented", "unknown_evidence"),
        ("g2", old.evidence_id, "wrong_goal"),
    ]:
        with pytest.raises(EvidenceError, match=reason):
            query.evidence.reference(goal, evidence_id)
    with pytest.raises(EvidenceError, match="unknown_evidence"):
        adapter(catalog).evidence.reference("g1", old.evidence_id)
    query.evidence.supersede("g1", old.evidence_id, new.evidence_id)
    with pytest.raises(EvidenceError, match="superseded"):
        query.evidence.reference("g1", old.evidence_id, require_complete=True)
    with pytest.raises(EvidenceError, match="superseded"):
        query.evidence.presentation("g1", old.evidence_id)
    assert query.evidence.reference("g1", new.evidence_id, require_complete=True)


async def test_read_copies_cannot_rewrite_facts_or_cards(monkeypatch):
    query = adapter(make_catalog(monkeypatch))
    record = await order_record(query)
    record.result.output["order"]["id"] = 123
    record.invocation.arguments["order_id"] = 123
    presentation = query.evidence.presentation("g1", record.evidence_id)
    assert presentation["candidates"]["order"]["id"] == 8201
    presentation["candidates"]["order"]["id"] = 456
    presentation["evidence"]["result"]["output"]["order"]["id"] = 456
    reference = query.evidence.reference("g1", record.evidence_id)
    assert reference.result.output["order"]["id"] == 8201
    assert reference.invocation.arguments["order_id"] == 8201
    assert query.attempts(reference.invocation.operation_id)[0].result.output["order"]["id"] == 8201


@pytest.mark.parametrize("output,blocker", [
    (OrderLookupOutput(result_type="order_count", total_match_count=8, is_exhaustive=False),
     "is_exhaustive"),
    (OrderLookupOutput(result_type="order_candidates", next_offset=5), "remaining_window"),
    (OrderLookupOutput(result_type="order_suggestions"), "domain_status:order_suggestions"),
    (OrderLookupOutput(result_type="planning_failed"), "execution_failed"),
    (OrderLookupOutput(result_type="unsupported"), "domain_status:unsupported"),
    (OrderLookupOutput(result_type="invalid_query"), "domain_status:invalid_query"),
    (OrderLookupOutput(result_type="not_found", diagnostics=[ToolDiagnostic(
        code="data_insufficient", severity="warning", message="缺字段",
    )]), "diagnostic:data_insufficient"),
])
async def test_ok_is_not_complete_and_restrictions_are_lossless(output, blocker):
    async def handler(request):
        return output

    contract = next(c for c in default_tool_contracts() if c.llm_name == "order_lookup")
    query = adapter(ToolCatalog([BoundTool(contract, handler)]))
    record = await order_record(query)
    if output.result_type == "planning_failed":
        assert not record.result.ok and record.result.output is None
        assert record.result.error.code == "planning_failed"
        assert record.result.error.next_action == "stop"
        assert record.completion_blockers == (blocker,)
        with pytest.raises(EvidenceError, match="execution_failed"):
            query.evidence.presentation("g1", record.evidence_id)
        with pytest.raises(EvidenceError, match="incomplete_evidence"):
            query.evidence.reference("g1", record.evidence_id, require_complete=True)
        return
    assert record.result.ok
    expected = output.model_dump(mode="json", exclude={"output_budget"})
    assert {k:v for k,v in record.result.output.items() if k != "output_budget"} == expected
    assert not record.result.output["output_budget"]["truncated"]
    assert blocker in record.completion_blockers
    with pytest.raises(EvidenceError, match="incomplete_evidence"):
        query.evidence.reference("g1", record.evidence_id, require_complete=True)
    presentation = query.evidence.presentation("g1", record.evidence_id)
    assert blocker in presentation["evidence"]["completion_blockers"]
    assert presentation["evidence"]["result"]["output"] == record.result.output


@pytest.mark.parametrize("name", ["policy_search", "knowledge_search"])
async def test_retrieval_sources_are_not_exhaustive_or_authorizing(monkeypatch, name):
    query = adapter(make_catalog(monkeypatch))
    record = await query.execute(query.prepare("g1", name, {"query": "退货配件接口"}))
    assert record.result.output["documents"]
    assert "retrieval_not_exhaustive" in record.completion_blockers
    with pytest.raises(EvidenceError, match="incomplete_evidence"):
        query.evidence.reference("g1", record.evidence_id, require_complete=True)
    assert query.evidence.presentation("g1", record.evidence_id)["candidates"]["documents"]


async def test_error_can_explain_failure_but_never_supply_cards_or_completion(monkeypatch):
    catalog = make_catalog(monkeypatch)

    async def failed(request):
        raise RuntimeError("synthetic secret must not be echoed")

    contract = catalog.get_by_llm_name("order_lookup").contract
    query = adapter(ToolCatalog([BoundTool(contract, failed)]))
    record = await order_record(query)
    assert record.result.error.code == "internal_error"
    assert record.result.error.next_action == "stop"
    assert "secret" not in str(record.observation())
    assert query.evidence.reference("g1", record.evidence_id).result.error
    with pytest.raises(EvidenceError, match="incomplete_evidence"):
        query.evidence.reference("g1", record.evidence_id, require_complete=True)
    with pytest.raises(EvidenceError, match="execution_failed"):
        query.evidence.presentation("g1", record.evidence_id)


async def test_prerequisite_guard_requires_actual_evidence_before_downstream(monkeypatch):
    prerequisite_ids = []

    def guard(goal_id):
        if goal_id == "g1":
            return
        if goal_id != "g2" or not prerequisite_ids:
            raise AdmissionError("prerequisite_missing")
        try:
            for evidence_id in prerequisite_ids:
                query.evidence.reference("g1", evidence_id, require_complete=True)
        except EvidenceError as exc:
            raise AdmissionError("prerequisite_invalid") from exc
        # A real P2 conditional goal must also evaluate its predicate, not just this reference.

    query = QueryToolAdapter(
        make_catalog(monkeypatch), ToolRuntimeContext(user_id=41), admit_goal=guard,
    )
    with pytest.raises(AdmissionError, match="prerequisite_missing"):
        query.prepare("g2", "order_lookup", {"order_id": 8201})
    prerequisite_ids.append("ev_invented")
    with pytest.raises(AdmissionError, match="prerequisite_invalid"):
        query.prepare("g2", "order_lookup", {"order_id": 8201})
    record = await order_record(query)
    prerequisite_ids[:] = [record.evidence_id]
    downstream = query.prepare("g2", "order_lookup", {"order_id": 8201})
    replacement = await order_record(query)
    query.evidence.supersede("g1", record.evidence_id, replacement.evidence_id)
    with pytest.raises(AdmissionError, match="prerequisite_invalid"):
        await query.execute(downstream)
    assert query.attempts(downstream) == ()
