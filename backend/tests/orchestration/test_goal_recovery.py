"""Recovery through the production graph, scripted models and no external I/O."""

import asyncio
import json
from copy import deepcopy

import pytest

from app.agent.answer_context import build_answer_context
from app.agent.goal_planning import ExecutionPlan, GoalPlan, PlanValidationError
from app.agent.prompts.planning import GOAL_ROUTER_PROMPT, TASK_PLANNER_PROMPT
from app.agent.recovery import admit_recovery, merged_execution, validate_route_scope
from app.schemas.chat import ChatRequest
from app.tools.schemas import ToolError, ToolExecutionResult
from tests.support.runtime import goal, message, setup_runtime, task


def request(query, identity="goal_1", stage="llm2", tasks=None):
    return {"goal_id": identity, "source_text": query, "task_ids": tasks or [],
            "target_stage": stage, "evidence": "可见问题与目标不一致",
            "hint": "保留原条件，修正本目标的处理方式"}


def answer(ids, goals):
    return message("finish_answer", {"response": "已查到这些商品的价格。",
                                     "used_tool_call_ids": ids, "covered_goal_ids": goals})


def fake_search(executor, *, fail_query=None):
    calls = []

    async def execute(contract, arguments, trusted):
        calls.append(deepcopy(arguments))
        if arguments.get("query") == fail_query:
            return ToolExecutionResult(tool_name=contract.name, ok=False, error=ToolError(
                code="invalid_input", message="synthetic invalid input", retryable=True,
                recommended_action="replan_arguments",
            ))
        product = executor.search.repository.rows[0].product.model_dump(mode="json")
        return ToolExecutionResult(tool_name=contract.name, ok=True,
                                   output={"result_type": "products", "products": [product],
                                           "query_plan": {}})

    executor.execute = execute
    return calls


@pytest.mark.asyncio
async def test_one_batch_reuses_both_nodes_preserves_success_and_joins_once(monkeypatch):
    queries = ["查询SKU 1价格", "查询SKU 2价格", "查询SKU 3价格"]
    goals = [goal(q, f"goal_{i}") for i, q in enumerate(queries, 1)]
    goals[2]["disposition"] = "direct_response"
    goals[2]["query"] = "键盘是什么"
    runtime, model, executor, context = setup_runtime(monkeypatch, "；".join(queries), goals, [
        {"goal_id": "goal_1", "tasks": [task(queries[0])]},
        {"goal_id": "goal_2", "tasks": [task(queries[1], "task_2")]},
    ])
    calls = fake_search(executor, fail_query=queries[1])
    model.scripts["route_request"].append(message("route_request", {
        "rewritten_query": queries[2], "goals": [goal(queries[2], "goal_3")],
    }))
    model.scripts["submit_execution_plan"].append(message("submit_execution_plan", {
        "tasks": [task("读取SKU 2的价格", "task_2", goal_id="goal_2"),
                  task(queries[2], "task_3", goal_id="goal_3")], "blocked_goals": [],
    }))
    model.scripts["finish_answer"] = [message("request_recovery", {"requests": [
        request(queries[1], "goal_2", tasks=["task_2"]),
        request(queries[2], "goal_3", "llm1"),
    ]}), answer(["planned_task_1", "planned_task_3", "planned_task_4"],
               ["goal_1", "goal_2", "goal_3"])]
    initial = runtime._initial_state(ChatRequest(message="；".join(queries)), 1)
    state, visited = {}, []
    async for update in runtime._build_graph().astream(initial, stream_mode="updates"):
        for node, values in update.items():
            visited.append(node)
            state.update(values)
    assert visited.count("request_router") == visited.count("plan_tools") == 2
    assert visited.count("persist_turn") == context.completed == 1
    assert len(calls) == 4 and calls.count({"query": queries[0]}) == 1
    assert state["terminal_guard_status"] == "accepted"
    assert state["recovery_batch"]["phase"] == "done"
    assert all(i["status"] == "settled" for i in state["recovery_batch"]["items"])
    assert state["task_artifacts"]["task_1"]["source_tool_call_id"] == "planned_task_1"
    assert "task_2" not in state["task_artifacts"]
    assert next(e for e in state["subquery_ledger"]
                if e["tool_call_id"] == "planned_task_2")["status"] == "superseded"
    for role, original in [("route_request", GOAL_ROUTER_PROMPT),
                           ("submit_execution_plan", TASK_PLANNER_PROMPT)]:
        assert model.calls[role][0][0].content == original
        assert model.calls[role][1][0].content.startswith(original)
        payload = json.loads(model.calls[role][1][1].content)
        assert "recovery_context" in payload
    assert "request_recovery" in model.calls["finish_answer"][0][0].content
    assert "<recovery_option>" not in model.calls["finish_answer"][1][0].content


@pytest.mark.asyncio
async def test_non_tool_entry_can_recover_to_tools(monkeypatch):
    query = "查询SKU 1价格"
    runtime, model, executor, context = setup_runtime(monkeypatch, query, [
        goal(query, disposition="direct_response"),
    ], [])
    calls = fake_search(executor)
    model.scripts["summarize_goals"] = [message("summarize_goals", {
        "recovery_requests": [request(query, stage="llm1")],
    })]
    model.scripts["route_request"].append(message("route_request", {
        "rewritten_query": query, "goals": [goal(query)],
    }))
    model.scripts["submit_execution_plan"] = [message("submit_execution_plan", {
        "tasks": [task(query, goal_id="goal_1")], "blocked_goals": [],
    })]
    model.scripts["finish_answer"] = [answer(["planned_task_1"], ["goal_1"])]
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1),
    )
    assert len(calls) == context.completed == 1
    assert state["terminal_guard_status"] == "accepted"
    assert state["answer_call_count"] == 2


@pytest.mark.asyncio
async def test_repeated_plan_cannot_reset_failed_call_budget(monkeypatch):
    query = "查询SKU 1价格"
    runtime, model, executor, context = setup_runtime(monkeypatch, query, [goal(query)], [
        {"goal_id": "goal_1", "tasks": [task(query)]},
    ])
    calls = fake_search(executor, fail_query=query)
    for _ in range(2):
        model.scripts["submit_execution_plan"].append(message("submit_execution_plan", {
            "tasks": [task(query, "task_8", goal_id="goal_1")], "blocked_goals": [],
        }))
    model.scripts["finish_answer"] = [message("request_recovery", {
        "requests": [request(query, tasks=["task_1"])],
    }), message("finish_unavailable", {"response": "该商品的价格暂时未能查到。",
                                      "covered_goal_ids": ["goal_1"],
                                      "unavailable_parts": [query]})]
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1),
    )
    assert len(calls) == 1 and context.completed == 1
    assert state["recovery_batch"]["items"][0]["reason"].startswith("recovery_planning_failed")
    assert state["task_status"]["task_1"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_guard_partially_accepts_and_invalidates_transitive_consumers(monkeypatch):
    from tests.support.answer_state import result_state

    state = result_state("catalog_search", error={
        "code": "invalid_input", "message": "bad", "retryable": True,
        "recommended_action": "replan_arguments",
    }, dependency=True)
    query = "查询当前条件下的结果"
    state["message"] = query
    state["route_plan"]["subqueries"][0]["source_text"] = query
    state["execution_plan"] = ExecutionPlan(tasks=[
        task(query, goal_id="goal_1"),
        task("读取结果规格", "task_2", tool="catalog_facets",
             goal_id="goal_1", depends_on=["task_1"]),
    ]).model_dump(mode="json")
    admit_recovery(state, [request(query, tasks=["task_1"]), request("不属于用户的话", "goal_7")])
    assert state["recovery_batch"]["affected_task_ids"] == ["task_1", "task_2"]
    assert state["recovery_batch"]["items"][1]["reason"] == "source_not_in_user_message"
    assert state["recovery_batch"]["phase"] == "llm2"
    assert build_answer_context(state)["recovery_policy"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "planning_output_invalid", "planning_review_inconclusive", "timeout", "forbidden",
    "invalid_plan",
])
async def test_guard_rejects_exhaustion_and_permissions(reason):
    from tests.support.answer_state import result_state

    state = result_state("catalog_search", error={"code": reason, "message": "error"})
    query = "查询当前条件下的结果"
    state["message"] = query
    state["route_plan"]["subqueries"][0]["source_text"] = query
    state["execution_plan"] = ExecutionPlan(tasks=[task(query, goal_id="goal_1")]).model_dump()
    admit_recovery(state, [request(query, tasks=["task_1"])])
    assert state["recovery_batch"]["phase"] == "done"
    assert state["recovery_batch"]["items"][0]["reason"] == "exhausted_or_forbidden_call"


@pytest.mark.asyncio
async def test_no_budget_never_dispatches_recovery():
    state = {"message": "你好", "route_plan": {"rewritten_query": "你好", "subqueries": [
        {**goal("你好", disposition="direct_response"), "reason_code": "test", "tasks": []},
    ]}, "turn_deadline_monotonic": asyncio.get_running_loop().time() + 1}
    admit_recovery(state, [request("你好", stage="llm1")])
    assert state["recovery_batch"]["phase"] == "done"
    assert state["recovery_batch"]["items"][0]["reason"] == "recovery_not_available"


def test_router_cannot_replace_unrelated_goal():
    with pytest.raises(PlanValidationError):
        validate_route_scope(GoalPlan(rewritten_query="刷新率", goals=[goal("刷新率", "goal_2")]), {
            "goals": [goal("刷新率")], "available_goal_ids": [],
        })


def test_planner_cannot_change_protected_task():
    original = {**task("查询SKU 1价格", goal_id="goal_1"), "depends_on": [], "bindings": [],
                "require_exact_dependencies": True, "answer_role": "user_facing"}
    state = {"recovery_batch": {"llm2_goal_ids": ["goal_1"], "protected_tasks": [original]}}
    with pytest.raises(PlanValidationError, match="preserve protected"):
        merged_execution(state, ExecutionPlan(tasks=[task("其他商品", goal_id="goal_1")]))


@pytest.mark.asyncio
async def test_missing_goal_added_without_rewriting_successful_goal(monkeypatch):
    query = "你好；刷新率是什么意思"
    runtime, model, executor, context = setup_runtime(monkeypatch, query, [
        goal("你好", disposition="direct_response"),
    ], [])
    model.scripts["summarize_goals"] = [message("summarize_goals", {
        "recovery_requests": [request("刷新率是什么意思", None, "llm1")],
    }), message("summarize_goals", {
        "response": "你好，刷新率是屏幕每秒更新画面的次数。",
        "covered_goal_ids": ["goal_1", "goal_2"],
    })]
    model.scripts["route_request"].append(message("route_request", {
        "rewritten_query": "刷新率是什么意思",
        "goals": [goal("刷新率是什么意思", "goal_2", "direct_response")],
    }))
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1),
    )
    assert context.completed == 1 and not executor.calls
    assert state["route_plan"]["subqueries"][0]["query"] == "你好"
    assert len(state["route_plan"]["subqueries"]) == 2
    assert state["terminal_guard_status"] == "accepted"
    assert len(model.calls["route_request"]) == 2
    assert not model.calls["submit_execution_plan"]


@pytest.mark.asyncio
async def test_llm1_recovery_can_split_selected_goal_with_stable_original_id(monkeypatch):
    query = "刷新率和分辨率是什么意思"
    runtime, model, executor, context = setup_runtime(monkeypatch, query, [
        goal(query, disposition="direct_response"),
    ], [])
    model.scripts["summarize_goals"] = [message("summarize_goals", {
        "recovery_requests": [request(query, stage="llm1")],
    }), message("summarize_goals", {
        "response": "刷新率是每秒刷新次数；分辨率描述画面的像素数量。",
        "covered_goal_ids": ["goal_1", "goal_2"],
    })]
    model.scripts["route_request"].append(message("route_request", {
        "rewritten_query": query, "goals": [
            goal("刷新率", disposition="direct_response"),
            goal("分辨率", "goal_2", "direct_response"),
        ],
    }))
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1),
    )
    assert context.completed == 1 and not executor.calls
    assert len(state["recovery_batch"]["items"]) == 2
    assert all(i["status"] == "settled" for i in state["recovery_batch"]["items"])
    assert state["terminal_guard_status"] == "accepted"


@pytest.mark.asyncio
async def test_final_summary_cannot_start_a_second_batch(monkeypatch):
    query = "刷新率是什么意思"
    runtime, model, executor, context = setup_runtime(monkeypatch, query, [
        goal(query, disposition="direct_response"),
    ], [])
    recover = message("summarize_goals", {"recovery_requests": [request(query, stage="llm1")]})
    model.scripts["summarize_goals"] = [recover, recover, recover]
    model.scripts["route_request"].append(message("route_request", {
        "rewritten_query": query, "goals": [goal(query, disposition="direct_response")],
    }))
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1),
    )
    assert context.completed == 1 and not executor.calls
    assert state["answer_call_count"] == 3
    assert len(model.calls["route_request"]) == 2
    assert state["terminal_guard_status"] == "fallback"
    assert state["answer"]


@pytest.mark.asyncio
async def test_recovery_keeps_valid_sibling_task_within_same_goal(monkeypatch):
    query = "查询SKU 1和SKU 2价格"
    runtime, model, executor, context = setup_runtime(monkeypatch, query, [goal(query)], [
        {"goal_id": "goal_1", "tasks": [task("查询SKU 1价格"), task("查询SKU 2价格", "task_2")]},
    ])
    calls = fake_search(executor, fail_query="查询SKU 2价格")
    model.scripts["submit_execution_plan"].append(message("submit_execution_plan", {
        "tasks": [task("查询SKU 1价格", goal_id="goal_1"),
                  task("读取SKU 2的价格", "task_2", goal_id="goal_1")],
        "blocked_goals": [],
    }))
    model.scripts["finish_answer"] = [message("request_recovery", {
        "requests": [request(query, tasks=["task_2"])],
    }), answer(["planned_task_1", "planned_task_3"], ["goal_1"])]
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1),
    )
    assert len(calls) == 3 and context.completed == 1
    assert calls.count({"query": "查询SKU 1价格"}) == 1
    assert state["terminal_guard_status"] == "accepted"


def test_bound_arguments_cannot_replay_invalidated_call():
    from app.agent.recovery import repeats_invalidated_call

    state = {"recovery_batch": {"affected_task_ids": ["task_1"]}, "tool_waves": [{
        "calls": [{"subquery": "task_1", "name": "catalog_facets",
                   "arguments": {"query": "规格", "scope": {"sku_ids": [1]}}}],
    }]}
    assert repeats_invalidated_call(state, "catalog_facets", {
        "query": "规格", "scope": {"sku_ids": [1]},
    })
    assert not repeats_invalidated_call(state, "catalog_facets", {
        "query": "规格", "scope": {"sku_ids": [2]},
    })


@pytest.mark.asyncio
async def test_recovery_tool_deadline_stops_and_still_finalizes(monkeypatch):
    monkeypatch.setattr("app.agent.recovery.RECOVERY_SECONDS", 0.03)
    query = "查询SKU 1价格"
    runtime, model, executor, context = setup_runtime(monkeypatch, query, [goal(query)], [
        {"goal_id": "goal_1", "tasks": [task(query)]},
    ])
    calls = []

    async def execute(contract, arguments, trusted):
        calls.append(arguments)
        if arguments["query"] != query:
            await asyncio.sleep(5)
        return ToolExecutionResult(tool_name=contract.name, ok=False, error=ToolError(
            code="invalid_input", message="synthetic failure", retryable=True,
            recommended_action="replan_arguments",
        ))

    executor.execute = execute
    model.scripts["submit_execution_plan"].append(message("submit_execution_plan", {
        "tasks": [task("读取SKU 1的价格", goal_id="goal_1")], "blocked_goals": [],
    }))
    model.scripts["finish_answer"] = [message("request_recovery", {
        "requests": [request(query, tasks=["task_1"])],
    }), message("finish_unavailable", {
        "response": "暂时未能查到价格。", "covered_goal_ids": ["goal_1"],
        "unavailable_parts": [query],
    })]
    state = await asyncio.wait_for(runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1),
    ), timeout=1)
    assert context.completed == 1 and len(calls) == 2
    assert state["tool_results"][-1]["execution"]["error"]["code"] == "recovery_budget_exhausted"
    assert state["recovery_batch"]["phase"] == "done"
    assert len(model.calls["finish_answer"]) == 2


@pytest.mark.asyncio
async def test_two_wave_recovery_rebinds_consumers_and_excludes_old_product(monkeypatch):
    query = "查询SKU 1的商品信息和规格选项"
    dependency = {"depends_on": ["task_1"], "bindings": [{
        "source": "task_output", "ref": "task_1", "kind": "product",
    }]}
    runtime, model, executor, context = setup_runtime(monkeypatch, query, [goal(query)], [
        {"goal_id": "goal_1", "tasks": [
            task("查询SKU 1的信息", answer_role="internal"),
            task("查询该商品的规格选项", "task_2", tool="catalog_facets", **dependency),
        ]},
    ])
    calls = []

    async def execute(contract, arguments, trusted):
        calls.append((contract.name, deepcopy(arguments)))
        if contract.name == "catalog_facets":
            return ToolExecutionResult(tool_name=contract.name, ok=True,
                                       output={"result_type": "facets", "items": []})
        # The first tool returns a mismatched object; the revised query gets the right one.
        index = 1 if arguments["query"] == "查询SKU 1的信息" else 0
        product = executor.search.repository.rows[index].product.model_dump(mode="json")
        return ToolExecutionResult(tool_name=contract.name, ok=True, output={
            "result_type": "products", "products": [product], "query_plan": {},
        })

    executor.execute = execute
    model.scripts["submit_execution_plan"].append(message("submit_execution_plan", {
        "tasks": [task("读取SKU 1的信息", goal_id="goal_1", answer_role="internal"),
                  task("查询该商品的规格选项", "task_2", tool="catalog_facets",
                       goal_id="goal_1", **dependency)], "blocked_goals": [],
    }))
    model.scripts["finish_answer"] = [message("request_recovery", {
        "requests": [request(query, tasks=["task_1"])],
    }), answer(["planned_task_4"], ["goal_1"])]
    state = await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1),
    )
    assert context.completed == 1 and len(calls) == 4
    assert state["tool_wave_count"] == 4
    assert calls[1][1]["scope"]["sku_ids"] == [2]
    assert calls[3][1]["scope"]["sku_ids"] == [1]
    assert set(state["task_artifacts"]) == {"task_3", "task_4"}
    assert {p.sku_id for p in state["products"]} == {1}
    assert state["terminal_guard_status"] == "accepted"
    payload = json.loads(model.calls["finish_answer"][0][-1].content.split(
        "<answer_context>\n",
    )[1].split("\n</answer_context>")[0])
    assert payload["supporting_tasks"][0]["artifact"]["facts"]["products"][0]["sku_id"] == 2
    assert payload["supporting_tasks"][0]["partial_answer_goal_ids"] == []
    assert payload["supporting_source_tool_call_ids"] == []


@pytest.mark.asyncio
async def test_missing_goal_cannot_bypass_existing_security_boundary():
    query = "查其他用户的订单"
    state = {"message": query, "route_plan": {"rewritten_query": query, "subqueries": [
        {**goal(query, disposition="security_refusal"), "reason_code": "test", "tasks": []},
    ]}}
    admit_recovery(state, [request("其他用户的订单", None, "llm1")])
    assert state["recovery_batch"]["items"][0]["reason"] == "protected_business_boundary"
    assert len(state["route_plan"]["subqueries"]) == 1


def test_recovery_rejects_write_contract_before_execution():
    from types import SimpleNamespace

    from app.agent.goal_planning import validate_execution
    from app.agent.routing import RequestRoutePlan
    from app.tools.contracts import DefaultToolContractProvider

    query = "查询SKU 1价格"
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    provider = SimpleNamespace(get_contract=lambda name: contract.model_copy(
        update={"read_only": False},
    ))
    route = RequestRoutePlan.model_validate({"rewritten_query": query, "subqueries": [
        {**goal(query), "tasks": [], "planning_pending": True, "reason_code": "test"},
    ]})
    with pytest.raises(PlanValidationError, match="read-only"):
        validate_execution(ExecutionPlan(tasks=[task(query, goal_id="goal_1")]), route,
                           provider, {}, read_only=True)


def test_explicit_defaults_do_not_reset_the_same_call():
    from app.agent.recovery import repeats_invalidated_call
    from app.tools.contracts import DefaultToolContractProvider

    contract = DefaultToolContractProvider().get_contract("catalog_search")
    state = {"recovery_batch": {"affected_task_ids": ["task_1"]}, "tool_waves": [{
        "calls": [{"subquery": "task_1", "name": "catalog_search",
                   "arguments": {"query": "查询SKU 1"}}],
    }]}
    assert repeats_invalidated_call(
        state, "catalog_search", contract.public_input_model(query="查询SKU 1").model_dump(),
        contract.public_input_model,
    )
