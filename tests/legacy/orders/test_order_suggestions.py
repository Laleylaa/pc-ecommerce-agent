import pytest
from app.agent.legacy.goal_planning import ToolTask, bound_inputs, context_objects
from app.agent.legacy.projections import rebuild_tool_projections
from app.agent.tool_outcomes import normalize_tool_result
from app.schemas.context import WorkingMemoryV2
from app.schemas.order import OrderFilters
from app.services.context import _next_working_memory

from tests.support.orders_order_suggestions import lookup as lookup
from tests.support.orders_order_suggestions import (
    september,
)


@pytest.mark.asyncio
async def test_incomplete_description_is_still_exact_and_window_can_bind(lookup):
    run, calls, _ = lookup
    result = await run(OrderFilters(product_terms=["K87"]), limit=1)
    assert result.total_match_count == 2 and not result.is_exhaustive
    assert result.focus_order_ids == [102] and not result.suggestions
    task = ToolTask(id="task_2", goal_id="goal_1", tool_name="order_lookup",
                    arguments={"query": "按下单时间最新一笔的物流"}, depends_on=["task_1"],
                    bindings=[{"source": "task_output", "ref": "task_1", "kind": "order"}])
    assert bound_inputs({"task_artifacts": {"task_1": {
        "usable": True, "value": result.model_dump(mode="json"),
    }}}, task) == [{"kind": "order", "id": 102}]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_candidates_are_preserved_for_question_but_not_bound_or_selected_in_memory(lookup):
    run, _, _ = lookup
    result = await run(september(statuses=[4]))
    value = result.model_dump(mode="json")
    execution = {"tool_call_id": "call_1", "name": "order_lookup",
                 "execution": {"ok": True, "output": value}}
    outcome = normalize_tool_result(execution)
    assert outcome.outcome == "insufficient"
    assert outcome.reason == "order_confirmation_required" and not outcome.has_usable_information
    # Projection must retain clarification facts even though they are not usable dependencies.
    state = {"task_artifacts": {"task_1": {
        "source_tool_name": "order_lookup", "usable": False, "value": value,
    }}}
    rebuild_tool_projections(state)
    memory = _next_working_memory(WorkingMemoryV2.model_validate({
        "order": {"last_order_id": 500, "candidate_order_ids": [500]},
    }), state)
    assert memory.order.last_order_id is None and memory.order.candidate_order_ids == []
    assert [s.id for s in memory.order.pending_suggestions] == [101]
    objects = context_objects({"working_memory_snapshot": memory.model_dump(mode="json")})
    assert set(objects) == {"order_suggestion_1"}
    assert objects["order_suggestion_1"]["requires_confirmation"] is True
    task = ToolTask(id="task_2", goal_id="goal_1", tool_name="order_lookup",
                    arguments={"query": "查物流"}, depends_on=["task_1"],
                    bindings=[{"source": "task_output", "ref": "task_1", "kind": "order"}])
    with pytest.raises(ValueError, match="usable facts"):
        bound_inputs(state, task)
    # Even an accidentally usable artifact must not turn suggestions into selected objects.
    state["task_artifacts"]["task_1"]["usable"] = True
    with pytest.raises(ValueError, match="confirmation"):
        bound_inputs(state, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_formal_execution_keeps_suggestions_for_clarification_and_blocks_dependency(
    lookup, monkeypatch, internal,
):
    from app.agent.legacy.answer_context import build_answer_context
    from app.agent.legacy.execution import execute_tool_wave
    from app.tools.contracts import BoundTool, RegistryToolExecutor, ToolCatalog

    from tests.support.planning_harness import goal, plan_request, task

    run, _, _ = lookup
    result = await run(september(statuses=[4]))
    query = "查九月买的已完成K87订单，找到后看物流"
    tasks = [task(query, tool_name="order_lookup",
                  answer_role="internal" if internal else "user_facing")]
    if internal:
        tasks.append(task("查找到的订单物流", "task_2", tool_name="order_lookup",
                          depends_on=["task_1"], bindings=[{
                              "source": "task_output", "ref": "task_1", "kind": "order",
                          }]))
    runtime, state, _, _, _ = await plan_request(query, [goal(query)], tasks)
    calls = []

    async def lookup_result(request):
        calls.append(request)
        return result

    class Audit:
        def __init__(self, session):
            pass

        async def add_tool_call(self, *args):
            pass

    runtime.tool_executor = RegistryToolExecutor(None, runtime.settings, catalog=ToolCatalog([
        BoundTool(runtime.contract_provider.get_contract("order_lookup"), lookup_result),
    ]))
    await runtime._orchestrate(state)
    await execute_tool_wave(runtime, state, repository_factory=Audit,
                            stream_writer_factory=lambda: lambda event: None)
    await runtime._normalize_tool_results(state)
    await runtime._extract_task_artifacts(state)
    await runtime._update_subquery_ledger(state)
    artifact = state["task_artifacts"]["task_1"]
    assert not artifact["usable"] and artifact["value"]["suggestions"][0]["order"]["id"] == 101
    assert state["task_status"]["task_1"]["status"] == "blocked"
    context = build_answer_context(state)
    assert context["recommended_control_action"] == "ask_clarification"
    if internal:
        assert state["task_status"]["task_2"]["status"] == "blocked"
        assert context["tasks"][0]["semantic_outcome"] == "needs_clarification"
        record = context["supporting_tasks"][0]
        assert record["clarification_goal_ids"] == ["goal_1"]
        assert record["partial_answer_goal_ids"] == []
    else:
        record = context["tasks"][0]
        assert record["handling"] == "clarify"
    assert record["artifact"]["facts"]["suggestions"][0]["differences"]
    assert any("待确认" in text for text in record["response_contract"]["required"])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_next_turn_can_explicitly_choose_pending_order(lookup):
    from tests.support.planning_harness import goal, plan_request, task

    run, _, _ = lookup
    result = await run(september(statuses=[4]))
    state = {"task_artifacts": {"task_1": {
        "source_tool_name": "order_lookup", "usable": False,
        "value": result.model_dump(mode="json"),
    }}}
    rebuild_tool_projections(state)
    memory = _next_working_memory(WorkingMemoryV2(), state).model_dump(mode="json")
    message = "是的，我记错月份了，就是你列出的那笔，查一下现在的物流"
    query = "查询订单101当前物流"
    _, planned, _, _, _ = await plan_request(
        message, [goal(query, source_text=message, context_refs=["order_suggestion_1"])],
        [task(query, tool_name="order_lookup", arguments={"query": query, "order_id": 101})],
        memory=memory,
    )
    assert "planning_error" not in planned
    assert planned["execution_plan"]["tasks"][0]["arguments"]["order_id"] == 101
    refreshed = await run(OrderFilters(), operation="detail", selection="explicit", order_id=101)
    assert refreshed.result_type == "single_order" and refreshed.order.id == 101
    confirmed_state = {"order": refreshed.order, "parsed": {
        "order_query": {"result_type": "single_order", "total_match_count": 1,
                        "returned_count": 1, "is_exhaustive": True},
    }}
    confirmed = _next_working_memory(WorkingMemoryV2.model_validate(memory), confirmed_state)
    assert confirmed.order.last_order_id == 101 and not confirmed.order.pending_suggestions
