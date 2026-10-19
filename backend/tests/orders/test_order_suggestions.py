"""Business queries using isolated order snapshots; no database or model calls."""
from datetime import datetime

import pytest

from app.agent.goal_planning import ToolTask, bound_inputs, context_objects
from app.agent.outcomes import normalize_tool_result
from app.agent.projections import rebuild_tool_projections
from app.repositories.orders import OrderSearchPage
from app.schemas.context import WorkingMemoryV2
from app.schemas.order import OrderCard, OrderFilters, OrderItemCard
from app.services.context import _next_working_memory
from app.tools.orders import OrderQueryPlan, OrderToolService
from app.tools.schemas import OrderLookupInput


def order(identity, *, month=8, status=4, name="K87 键盘", pay_at=None):
    return OrderCard(
        id=identity, created_at=datetime(2026, month, 15), status=status,
        status_label={2: "待发货", 3: "已发货", 4: "已完成"}[status], pay_amount=199,
        pay_at=pay_at, logistics=None,
        items=[OrderItemCard(id=identity, sku_id=87, sku_name=name,
                             sku_specs={"color": "白色"}, price=199, quantity=1)],
    )


@pytest.fixture
def lookup(monkeypatch):
    calls = []
    # Another user's same product must never leak through the relaxed search.
    rows = [(7, order(101)), (7, order(102, month=9, status=3)),
            (7, order(103, month=9, name="K98 键盘")), (8, order(999, month=9, status=2))]

    class Repository:
        def __init__(self, session):
            pass

        def matching(self, user_id, filters=None):
            filters = filters or OrderFilters()
            matched = []
            for owner, card in rows:
                timestamp = getattr(card, filters.time_field)
                if owner != user_id:
                    continue
                if filters.statuses and card.status not in filters.statuses:
                    continue
                if filters.start_at and (timestamp is None or timestamp < filters.start_at):
                    continue
                if filters.end_at and (timestamp is None or timestamp >= filters.end_at):
                    continue
                if not any(all(term in item.sku_name for term in filters.product_terms)
                           for item in card.items):
                    continue
                matched.append(card)
            return sorted(matched, key=lambda card: (card.created_at, card.id), reverse=True)

        async def list_recent_orders_page(self, user_id, *, limit, offset, filters=None):
            calls.append((user_id, filters))
            result = self.matching(user_id, filters)
            return OrderSearchPage(result[offset:offset + limit], len(result), offset)

        async def count_orders(self, user_id, *, filters=None):
            calls.append((user_id, filters))
            return len(self.matching(user_id, filters))

        async def latest_order(self, user_id, *, filters=None):
            result = self.matching(user_id, filters)
            return result[0] if result else None

        async def get_order(self, user_id, order_id, *, filters=None):
            calls.append((user_id, filters))
            return next((card for card in self.matching(user_id, filters)
                         if card.id == order_id), None)

    monkeypatch.setattr("app.tools.orders.OrderRepository", Repository)

    async def run(filters, *, operation="match_orders", selection="all", limit=5,
                  unresolved=(), order_id=None):
        class Planner:
            async def plan(self, request):
                return OrderQueryPlan(
                    operation=operation, selection=selection, filters=filters,
                    requested_fields=["order_count" if operation == "total_count"
                                      else "purchase_history_match"],
                    unresolved_requirements=list(unresolved),
                )
        return await OrderToolService(None, planner=Planner()).lookup(OrderLookupInput(
            user_id=7, query="查我之前买的K87订单及已有物流", limit=limit, order_id=order_id,
        ))
    return run, calls, rows


def september(**kwargs):
    return OrderFilters(product_terms=["K87"], start_at=datetime(2026, 9, 1),
                        end_at=datetime(2026, 10, 1), **kwargs)


@pytest.mark.asyncio
async def test_exact_product_and_month_result_needs_no_confirmation(lookup):
    run, calls, _ = lookup
    result = await run(september())
    assert result.result_type == "order_analysis"
    assert result.focus_order_ids == [102]
    assert not result.suggestions and not result.diagnostics
    assert len(calls) == 1


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
@pytest.mark.parametrize("selection", ["all", "latest"])
async def test_misremembered_month_preserves_product_and_status(lookup, selection):
    run, calls, _ = lookup
    result = await run(september(statuses=[4]), selection=selection)
    assert result.result_type == "order_suggestions"
    assert result.total_match_count == result.returned_count == 0
    assert result.focus_order_ids == [] and result.analysis_orders == []
    assert [s.order.id for s in result.suggestions] == [101]
    difference = result.suggestions[0].differences[0]
    assert difference.field == "time_range" and difference.state == "not_matched"
    assert difference.actual == "2026-08-15T00:00:00"
    assert result.suggestion_filters.statuses == [4]
    assert all(user == 7 and f.product_terms == ["K87"] for user, f in calls)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_misremembered_status_preserves_product_and_month(lookup):
    run, calls, _ = lookup
    result = await run(september(statuses=[2]))
    assert result.result_type == "order_suggestions"
    assert [s.order.id for s in result.suggestions] == [102]  # excludes user 8 and K98
    difference = result.suggestions[0].differences[0]
    assert difference.field == "statuses" and difference.expected == {"statuses": [2]}
    assert difference.actual == 3 and difference.state == "not_matched"
    assert result.suggestion_filters.start_at == datetime(2026, 9, 1)
    assert len(calls) == 3  # strict, time-only relaxation empty, status-only relaxation


@pytest.mark.asyncio
async def test_missing_payment_timestamp_is_unknown_not_wrong_date(lookup):
    run, _, _ = lookup
    result = await run(september(time_field="pay_at"))
    assert result.result_type == "order_suggestions"
    assert all(s.differences[0].state == "unknown" and s.differences[0].actual is None
               for s in result.suggestions)


@pytest.mark.asyncio
async def test_suggestion_window_does_not_change_exact_count(lookup):
    run, _, _ = lookup
    result = await run(september(time_field="pay_at"), limit=1)
    assert result.suggestion_total_count == 2
    assert len(result.suggestions) == 1 and not result.suggestion_is_exhaustive
    assert result.total_match_count == 0 and result.is_exhaustive


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["count", "unsupported", "explicit", "wrong_model", "no_product"])
async def test_no_silent_relaxation_of_unsupported_or_identity_requirements(lookup, case):
    run, calls, _ = lookup
    options = {}
    filters = september(statuses=[2])
    if case == "count":
        options["operation"] = "total_count"
    elif case == "unsupported":
        options["unresolved"] = ["周五前一定送到"]
    elif case == "explicit":
        options.update(order_id=999, selection="explicit")
    elif case == "wrong_model":
        filters = filters.model_copy(update={"product_terms": ["K999"]})
    else:
        options["operation"] = "list"
        filters = filters.model_copy(update={"product_terms": [], "statuses": [2]})
    result = await run(filters, **options)
    assert not result.suggestions
    assert result.result_type in {"not_found", "order_count", "unsupported"}
    assert len(calls) <= (3 if case == "wrong_model" else 1)


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
    from app.agent.answer_context import build_answer_context
    from app.agent.execution import execute_tool_wave
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
