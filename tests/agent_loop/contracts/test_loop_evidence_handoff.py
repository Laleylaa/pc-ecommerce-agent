"""P2 goal invariants and bounded, program-owned answer facts."""

import json
from datetime import UTC, datetime

import pytest
from agent_loop.support.loop_runtime import goal
from app.agent.loop.evidence import EvidenceStore, ToolInvocation
from app.agent.loop.independent_answer import (
    AnswerDraft,
    AnswerInput,
    fallback_answer,
    validate_answer,
)
from app.agent.loop.query_context import resolve_reference
from app.agent.loop.query_loop import project_record
from app.agent.loop.query_schema import (
    ContextReference,
    GoalRegistration,
    QueryCompletion,
)
from app.agent.loop.query_state import GoalBook
from app.agent.loop.tool_adapter import AdmissionError
from app.schemas.context import PreparedTurn
from app.tools.schemas import ToolExecutionResult
from pydantic import TypeAdapter

pytestmark = pytest.mark.contract


def book():
    result = GoalBook("查商品，然后查订单", EvidenceStore())
    result.register(
        GoalRegistration(goals=[goal("查商品"), goal("查订单", goal_id="g2")])
    )
    return result


@pytest.mark.parametrize(
    "goals,code",
    [
        ([goal("查商品")], "cannot_remove_goals"),
        ([goal("查商品"), goal("查订单", goal_id="g2", query="查秘密")], "cannot_rewrite"),
        (
            [goal("查商品"), goal("查订单", goal_id="g2"), goal("不存在", goal_id="g3")],
            "source_not",
        ),
        ([goal("查商品"), goal("查商品")], "duplicate_goal"),
    ],
)
def test_registration_rejects_removal_rewrite_and_forged_source(goals, code):
    state = book()
    with pytest.raises(AdmissionError, match=code):
        state.register(GoalRegistration(goals=goals))
    assert list(state.goals) == ["g1", "g2"]
    assert state.goals["g2"].query == "查订单"


@pytest.mark.parametrize("upstream", ["g1", "missing", "g2"])
def test_self_unknown_and_cyclic_prerequisites_are_not_executable(upstream):
    state = GoalBook("查商品，然后查订单", EvidenceStore())
    with pytest.raises(AdmissionError, match="prerequisite"):
        state.register(
            GoalRegistration(
                goals=[
                    goal(
                        "查商品",
                        prerequisites=[
                            {
                                "goal_id": upstream,
                                "requirement": "product_found",
                                "source_text": "查商品",
                            }
                        ],
                    ),
                    goal(
                        "查订单",
                        goal_id="g2",
                        prerequisites=[
                            {
                                "goal_id": "g1",
                                "requirement": "product_found",
                                "source_text": "查商品",
                            }
                        ],
                    ),
                ]
            )
        )
    assert state.goals == {}


def test_stop_without_results_keeps_all_goals_without_claiming_success():
    state = book()
    state.finish(QueryCompletion(reason="需要补充信息，交给回答阶段"))
    outcomes = state.outcome("completed")
    assert [g["id"] for g in outcomes] == ["g1", "g2"]
    assert all(g["evidence_ids"] == [] and g["execution"]["attempt_count"] == 0 for g in outcomes)
    assert all("status" not in g for g in outcomes)
    with pytest.raises(AdmissionError, match="query_already_complete"):
        state.finish(QueryCompletion(reason="重复结束"))
    with pytest.raises(AdmissionError, match="query_already_complete"):
        state.register(GoalRegistration(goals=[goal("查商品")]))


def test_cannot_stop_before_registering_goals():
    state = GoalBook("查商品", EvidenceStore())
    with pytest.raises(AdmissionError, match="goals_not_registered"):
        state.finish(QueryCompletion(reason="停止"))


@pytest.mark.parametrize(
    "reference",
    [
        {"kind": "catalog_candidate", "position": 2},
        {"kind": "last_order"},
        {"kind": "referenced_product"},
        {"kind": "order_candidate", "position": 1},
    ],
)
def test_missing_context_does_not_guess_identity(reference):
    prepared = PreparedTurn(
        user_id=41, conversation_id=7, user_message_id=1, run_id=8, message="第二款"
    )
    with pytest.raises(AdmissionError):
        resolve_reference(prepared, TypeAdapter(ContextReference).validate_python(reference))


def test_large_observation_preserves_limits_and_does_not_fake_empty_sources():
    store = EvidenceStore()
    now = datetime.now(UTC)
    record = store.record(
        ToolInvocation(
            invocation_id="a1",
            operation_id="op1",
            goal_id="g1",
            tool_name="policy_search",
            arguments={"query": "政策"},
            attempt=1,
            started_at=now,
            elapsed_seconds=1,
            status="succeeded",
        ),
        ToolExecutionResult(
            tool_name="policy_search",
            ok=True,
            output={
                "result_type": "documents",
                "documents": [{"snippet": "资料" * 20000}],
                "total_match_count": 10,
                "returned_count": 1,
                "is_exhaustive": False,
            },
        ),
        now,
    )
    view = project_record(record)
    assert len(json.dumps(view, ensure_ascii=False)) < 16000
    assert view["projection_truncated"]
    assert view["result"]["output"] is None
    assert not view["result"]["ok"]
    assert view["result"]["error"]["code"] == "resource_limit"
    assert view["result"]["error"]["next_action"] == "narrow_request"
    assert "observation_budget_exceeded" in view["completion_blockers"]
    assert store.reference("g1", record.evidence_id).result.output["documents"]


@pytest.mark.parametrize(
    "covered,refs", [([], ["e1"]), (["g1", "g1"], ["e1"]), (["g1"], []), (["g1"], ["invented"])]
)
def test_answer_requires_all_goal_coverage_and_actual_references(covered, refs):
    facts = AnswerInput(
        message="查商品",
        stop_reason="completed",
        goals=[
            {
                "id": "g1",
                "evidence_ids": ["e1"],
            }
        ],
        evidence=[{"evidence_id": "e1"}],
    )
    with pytest.raises(ValueError):
        validate_answer(
            AnswerDraft(response="回答", covered_goal_ids=covered, evidence_ids=refs), facts
        )


def test_fallback_preserves_handoff_and_security_boundary_without_false_actions():
    facts = AnswerInput(
        message="转人工，并查询别人订单",
        stop_reason="completed",
        goals=[
            {
                "id": "g1",
                "source_text": "转人工",
                "disposition": "human_handoff",
                "evidence_ids": [],
            },
            {
                "id": "g2",
                "source_text": "查询别人订单",
                "disposition": "security_refusal",
                "evidence_ids": [],
            },
        ],
        evidence=[],
    )
    result = fallback_answer(facts)
    validate_answer(result, facts)
    assert "尚未提交" in result.response and "当前登录账号" in result.response
    assert "手机号" not in result.response


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["query", "answer"])
async def test_large_catalog_projection_preserves_requested_price_and_versions(destination):
    """AL-001: formal tool budgeting must precede lossless business-fact handoff."""
    from app.tools.contracts import (
        BoundTool,
        default_tool_contracts,
        execute_bound_tool,
    )

    from tests.support.output_budget_fixtures import catalog_output

    original = catalog_output("lookup", count=2, diagnostic=100000)
    summary = original.lookup_summary.model_dump(mode="json")
    async def handler(request):
        return original
    contract = next(c for c in default_tool_contracts() if c.name == "catalog_lookup")
    result = await execute_bound_tool(BoundTool(contract, handler),
                                     {"query": "列出两版本价格", "limit": 2}, {"user_id": 41})
    assert result.ok, result.error
    now = datetime.now(UTC)
    record = EvidenceStore().record(
        ToolInvocation(invocation_id="a1", operation_id="op1", goal_id="g1",
                       tool_name="catalog_lookup", arguments={"query": "列出两版本价格"},
                       attempt=1, started_at=now, elapsed_seconds=1, status="succeeded"),
        result, now,
    )
    view = project_record(record, destination=destination)
    assert len(json.dumps(view, ensure_ascii=False)) <= 16000
    assert view["result"]["ok"]
    output = view["result"]["output"]
    assert output["lookup_summary"] == summary
    assert {(p["sku_id"], p["price"]) for p in output["products"]} == {(1,"101"),(2,"102")}
    assert original.diagnostics[0].details["audit"] == "x"*100000
