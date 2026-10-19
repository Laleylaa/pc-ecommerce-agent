"""Production LLM1 -> LLM2 regressions, stopping before any tool execution."""

import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.agent.goal_planning import GoalPlan, ToolTask, bind_arguments, context_objects
from app.agent.limits import PLANNING_CALL_POLICY
from tests.support.planning_harness import goal, output, plan_request, task


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["router", "planner"])
@pytest.mark.parametrize("field", ["context_objects", "working_memory"])
async def test_models_cannot_replace_historical_context(stage, field):
    message = "第二款多少钱"
    query = "查询 SKU 202 的价格"
    memory = {"catalog": {"candidate_sku_ids": [101, 202]}}
    original = deepcopy(memory)
    goals = [goal(query, source_text=message, context_refs=["product_2"])]
    tasks = [task(query)]
    if stage == "router":
        bad = output("route_request", rewritten_query=query, goals=goals, **{field: {}})
        replies = {"router_outputs": [bad, output(
            "route_request", rewritten_query=query, goals=goals,
        )]}
    else:
        bad = output("submit_execution_plan", tasks=tasks, **{field: {}})
        replies = {"planner_outputs": [bad, output("submit_execution_plan", tasks=tasks)]}
    _, state, router, planner, _ = await plan_request(
        message, goals, tasks, memory=memory, **replies,
    )
    assert "planning_error" not in state
    assert len(router.messages) == (2 if stage == "router" else 1)
    assert len(planner.messages) == (2 if stage == "planner" else 1)
    assert memory == original
    assert state["working_memory_snapshot"]["catalog"]["candidate_sku_ids"] == [101, 202]
    first = json.loads(router.messages[0][-1].content)
    second = json.loads(planner.messages[0][-1].content)
    assert set(first["context_objects"]) == {"product_1", "product_2"}
    assert second["context_objects"] == {
        "product_2": {"kind": "product", "entity_scope": "sku", "ids": [202]},
    }
    assert "scope" not in second["goals"][0]
    assert state["execution_plan"]["tasks"][0]["arguments"] == {"query": query}


def test_context_projection_is_detached_and_distinguishes_model_from_representative():
    memory = {"catalog": {
        "query_plan": {"selection_scope": "spu"}, "referenced_spu_id": 10,
        "candidate_display": [{
            "entity_scope": "spu", "spu_id": 10, "sku_id": 101,
            "title": "历史型号", "candidate_sku_ids": [101, 102],
        }],
    }}
    before = deepcopy(memory)
    objects = context_objects({"working_memory_snapshot": memory, "message": "查SKU999"})
    assert objects["product_1"]["ids"] == [10]
    assert objects["product_1"]["entity_scope"] == "spu"
    assert "scope" not in objects["product_1"]
    objects["product_1"]["candidate_sku_ids"].append(999)
    assert objects["current_product"]["candidate_sku_ids"] == [101, 102]
    assert memory == before
    assert "scope" not in GoalPlan.model_json_schema()["$defs"]["Goal"]["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", ["query", "scope", "relation"])
async def test_history_identity_requires_selected_reference_even_without_goal_scope(forged):
    query = "查询 SKU 101 的价格"
    args = {"query": query}
    if forged == "query":
        args["query"] = "查询SKU编号202的价格"
    elif forged == "scope":
        args["scope"] = {"sku_ids": [202]}
    else:
        args["query"] = "查询SPU编号10的版本"
    bad = output("submit_execution_plan", tasks=[task(query, arguments=args)])
    _, state, _, planner, _ = await plan_request(
        "第一款多少钱", [goal(query, source_text="第一款多少钱", context_refs=["product_1"])],
        [], memory={"catalog": {"candidate_sku_ids": [101, 202]}},
        planner_outputs=[bad, bad],
    )
    assert len(planner.messages) == 2
    assert "planning_error" in state and "execution_plan" not in state


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search"])
async def test_context_binding_cannot_contradict_query_identity(tool):
    query = "分别查看 SKU 101 和 SKU 202"
    bad = task(
        "查看 SKU 101",
        tool_name=tool,
        bindings=[
            {"source": "context", "ref": "product_2", "kind": "product"},
        ],
    )
    bad["arguments"]["scope"] = {"sku_ids": [101]}
    invalid = output("submit_execution_plan", tasks=[bad])
    runtime, state, router, planner, _ = await plan_request(
        "看看刚才两款",
        [
            goal(
                query,
                source_text="看看刚才两款",

                context_refs=["product_1", "product_2"],
            )
        ],
        [],
        memory={"catalog": {"candidate_sku_ids": [101, 202]}},
        planner_outputs=[invalid, invalid],
    )
    assert "planning_error" in state
    assert "execution_plan" not in state
    assert runtime._dispatch_planned(state) == "answer"
    assert len(router.messages) == 1 and len(planner.messages) == 2
    assert "binding" in str(state["planning_error"])


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["candidate_versions", "parent_model"])
async def test_unavailable_context_selection_repairs_before_delivery(selection):
    message = "查这款的其他版本"
    planned = task(
        message,
        bindings=[
            {"source": "context", "ref": "product_1", "kind": "product", "selection": selection},
        ],
    )
    blocked = {"goal_id": "goal_1", "blocked_reason": "可信上下文缺少版本或父型号编号"}
    runtime, state, _, planner, _ = await plan_request(
        message,
        [
            goal(
                "查 SKU 101 的其他版本",
                source_text=message,

                context_refs=["product_1"],
            )
        ],
        [],
        memory={"catalog": {"candidate_sku_ids": [101]}},
        planner_outputs=[
            output("submit_execution_plan", tasks=[planned]),
            output("submit_execution_plan", tasks=[], blocked_goals=[blocked]),
        ],
    )
    assert "planning_error" not in state
    assert len(planner.messages) == 2
    assert state["execution_plan"] == {"tasks": [], "blocked_goals": [blocked]}
    assert runtime._dispatch_planned(state) == "answer"
    assert "identities are unavailable" in planner.messages[1][-1].content


@pytest.mark.asyncio
async def test_context_references_do_not_force_mixed_search_scope():
    message = "查刚才两个商品"
    query = "以 SKU 101 为参照，在 SPU 20 中找白色版本"
    valid = output(
        "submit_execution_plan",
        tasks=[
            task(
                query,
                arguments={"query": query, "scope": {"spu_ids": [20]}},
                bindings=[
                    {"source": "context", "ref": ref, "kind": "product"}
                    for ref in ("product_1", "product_2")
                ],
            )
        ],
    )
    _, state, _, planner, _ = await plan_request(
        message,
        [goal(query, source_text=message, context_refs=["product_1", "product_2"])],
        [],
        memory={
            "catalog": {
                "candidate_display": [
                    {"sku_id": 101, "spu_id": 10, "title": "A", "entity_scope": "sku"},
                    {"sku_id": 202, "spu_id": 20, "title": "B", "entity_scope": "spu"},
                ]
            }
        },
        planner_outputs=[valid],
    )
    assert "planning_error" not in state and len(planner.messages) == 1
    planned = ToolTask.model_validate(state["execution_plan"]["tasks"][0])
    assert bind_arguments(state, planned, planned.arguments) == planned.arguments
    assert planned.arguments["scope"] == {"sku_ids": [], "spu_ids": [20]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection,expected_scope",
    [
        ("object", {"sku_ids": [101]}),
        ("candidate_versions", {"sku_ids": [101, 102]}),
        ("parent_model", {"spu_ids": [10]}),
    ],
)
@pytest.mark.parametrize("tool", ["catalog_search"])
@pytest.mark.parametrize("extra_explicit", [False, True])
async def test_available_context_selection_remains_deliverable(
    selection, expected_scope, tool, extra_explicit,
):
    message = "查看这款商品及它的其他版本"
    kind, ids = next(iter(expected_scope.items()))
    if extra_explicit:
        message += f"，以及 {kind[:3].upper()} 303"
        ids = [*ids, 303]
        expected_scope = {kind: ids}
    query = "查看 " + "、".join(f"{kind[:3].upper()} {i}" for i in ids) + " 的商品信息"
    arguments = {"query": query}
    arguments["scope"] = expected_scope
    for ref in ("product_1", "current_product"):
        _, state, _, planner, _ = await plan_request(
            message,
            [goal(query, source_text=message,  context_refs=[ref])],
            [task(query, tool_name=tool, arguments=arguments, bindings=[
                {"source": "context", "ref": ref, "kind": "product", "selection": selection},
            ])],
            memory={"catalog": {
                "referenced_sku_id": 101,
                "candidate_display": [{
                    "sku_id": 101, "spu_id": 10, "candidate_sku_ids": [101, 102],
                    "title": "A", "entity_scope": "sku",
                }],
            }},
        )
        assert "planning_error" not in state and len(planner.messages) == 1
        planned = ToolTask.model_validate(state["execution_plan"]["tasks"][0])
        assert bind_arguments(state, planned, planned.arguments) == planned.arguments
        assert planned.arguments["query"] == query
        scope = planned.arguments["scope"]
        assert scope[kind] == ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,tool,query",
    [
        ("product", "catalog_search", "找销量前两款键盘，再查看规格"),
        ("order", "order_lookup", "列出最近两笔订单，再查第二笔物流"),
    ],
)
async def test_unknown_upstream_identity_keeps_legal_dependency(kind, tool, query):
    first_query = "找销量前两款键盘" if kind == "product" else "列出最近两笔订单"
    second_query = "查看找到的两款的规格" if kind == "product" else "查第二笔的物流"
    second_tool = tool
    bindings = [
        {
            "source": "task_output",
            "ref": "task_1",
            "kind": kind,
            "positions": [1, 2] if kind == "product" else [2],
        }
    ]
    runtime, state, _, planner, _ = await plan_request(
        query,
        [goal(query)],
        [
            task(first_query, tool_name=tool, arguments={"query": first_query, "limit": 2}),
            task(
                second_query,
                "task_2",
                tool_name=second_tool,
                depends_on=["task_1"],
                bindings=bindings,
            ),
        ],
    )
    assert "planning_error" not in state and len(planner.messages) == 1
    assert runtime._dispatch_planned(state) == "execute"
    second = state["execution_plan"]["tasks"][1]
    assert second["arguments"] == {"query": second_query}
    assert second["bindings"][0]["ref"] == "task_1"
    assert second["depends_on"] == ["task_1"] and second["require_exact_dependencies"]
    assert state["task_artifacts"] == {}
    assert state["task_status"]["task_1"]["status"] == "ready"
    assert state["task_status"]["task_2"]["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", [
    "explicit_limit", "default_limit", "mixed_scopes", "order_limit", "order_default",
    "order_id", "order_query",
])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
async def test_impossible_future_bindings_are_repaired_before_delivery(defect, reverse, persistent):
    query = "找几款键盘，再查看其中商品信息"
    first = task("找键盘", answer_role="internal")
    if defect == "explicit_limit":
        first["arguments"]["limit"] = 1
    position = 4 if defect == "default_limit" else 2
    bindings = [{
        "source": "task_output", "ref": "task_1", "kind": "product",
        "positions": [position],
    }]
    if defect == "mixed_scopes":
        bindings = [
            {**bindings[0], "positions": [1], "selection": selection}
            for selection in ("candidate_versions", "parent_model")
        ]
    second = task("查看商品信息", "task_2", depends_on=["task_1"], bindings=bindings)
    if defect.startswith("order_"):
        query = "列出最近两笔订单，再查第二笔物流"
        arguments = {"query": "列出最近两笔订单"}
        if defect == "order_limit":
            arguments["limit"] = 1
        elif defect in {"order_id", "order_query"}:
            query = "查订单101，再查结果第二笔的物流"
            arguments = {"order_id": 101} if defect == "order_id" else {"query": "查订单101"}
        first = task(
            query, tool_name="order_lookup", arguments=arguments, answer_role="internal",
        )
        bindings = [{
            "source": "task_output", "ref": "task_1", "kind": "order",
            "positions": [6 if defect == "order_default" else 2],
        }]
        second = task(
            "查询返回的订单物流", "task_2", tool_name="order_lookup",
            depends_on=["task_1"], bindings=bindings,
        )
    corrected = {**second, "bindings": [{**bindings[0], "positions": [1]}]}
    ordered = [second, first] if reverse else [first, second]
    fixed = [corrected, first] if reverse else [first, corrected]
    invalid = output("submit_execution_plan", tasks=ordered)
    runtime, state, router, planner, _ = await plan_request(
        query, [goal(query)], [],
        planner_outputs=[invalid, invalid if persistent else output(
            "submit_execution_plan", tasks=fixed,
        )],
    )
    assert len(router.messages) == 1 and len(planner.messages) == 2
    feedback = planner.messages[1][-1].content
    assert "task_2" in feedback
    assert ("SKU and SPU" if defect == "mixed_scopes" else "limit") in feedback
    if persistent:
        assert "planning_error" in state and "execution_plan" not in state
        assert runtime._dispatch_planned(state) == "answer"
    else:
        assert "planning_error" not in state
        delivered = {t["id"]: t for t in state["execution_plan"]["tasks"]}
        assert delivered["task_1"]["arguments"] == first["arguments"]
        assert delivered["task_2"]["arguments"] == second["arguments"]
        assert delivered["task_2"]["bindings"][0]["positions"] == [1]
        assert runtime._dispatch_planned(state) == "execute"


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["object", "candidate_versions", "parent_model"])
async def test_future_binding_actual_result_checks_remain_deferred(selection):
    query = "找两款键盘，再查看其中的商品信息"
    _, state, _, planner, _ = await plan_request(
        query, [goal(query)], [
            task("找两款键盘", arguments={"query": "找两款键盘", "limit": 2}),
            task("查看商品信息", "task_2", depends_on=["task_1"], bindings=[
                {
                    "source": "task_output", "ref": "task_1", "kind": "product",
                    "positions": [2], "selection": selected,
                }
                for selected in (selection, "object")
            ]),
        ],
    )
    assert "planning_error" not in state and len(planner.messages) == 1
    planned = ToolTask.model_validate(state["execution_plan"]["tasks"][1])
    # limit is an upper bound: fewer actual objects are a runtime condition.
    state["task_artifacts"] = {"task_1": {"usable": True, "value": {"products": [{
        "entity_scope": "sku", "sku_id": 101, "spu_id": 10, "candidate_sku_ids": [101],
    }]}}}
    with pytest.raises(ValueError, match="outside result window"):
        bind_arguments(state, planned, planned.arguments)
    state["task_artifacts"]["task_1"]["value"]["products"].append({
        "entity_scope": "sku", "sku_id": 202, "spu_id": 20, "candidate_sku_ids": [202],
    })
    if selection == "parent_model":
        # object granularity was unknown during planning; only now is the conflict certain.
        with pytest.raises(ValueError, match="SKU and SPU"):
            bind_arguments(state, planned, planned.arguments)
    else:
        assert bind_arguments(state, planned, planned.arguments) == {
            "query": "查看商品信息", "scope": {"sku_ids": [202]},
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_explicit_order_query_cannot_be_overridden_by_future_binding(persistent):
    message = "查订单123，再查最近一笔订单的物流"
    first = task("查最近一笔订单", tool_name="order_lookup")
    second = task(
        "查订单123的物流", "task_2", tool_name="order_lookup",
        depends_on=["task_1"],
        bindings=[{"source": "task_output", "ref": "task_1", "kind": "order"}],
    )
    invalid = output("submit_execution_plan", tasks=[first, second])
    corrected = {**second, "arguments": {"query": "查刚才查到的订单的物流"}}
    _, state, router, planner, _ = await plan_request(
        message, [goal(message)], [],
        planner_outputs=[
            invalid,
            invalid if persistent else output("submit_execution_plan", tasks=[first, corrected]),
        ],
    )
    assert len(router.messages) == 1 and len(planner.messages) == 2
    assert "task_output" in planner.messages[1][-1].content
    if persistent:
        assert "planning_error" in state and "execution_plan" not in state
    else:
        assert "planning_error" not in state
        planned = ToolTask.model_validate(state["execution_plan"]["tasks"][1])
        state["task_artifacts"] = {
            "task_1": {"usable": True, "value": {"order": {"id": 456}}},
        }
        assert bind_arguments(state, planned, planned.arguments) == {
            "query": "查刚才查到的订单的物流", "order_id": 456,
        }


@pytest.mark.asyncio
async def test_independent_search_and_facets_do_not_gain_dependencies_or_scope():
    message = "查第二款库存，再看看全店有哪些键盘布局"
    query = "查 SKU 202 库存，再看看全店有哪些键盘布局"
    _, state, _, planner, _ = await plan_request(
        message,
        [goal(query, source_text=message,  context_refs=["product_2"])],
        [
            task(
                "查 SKU 202 库存",
                arguments={"query": "查 SKU 202 库存", "scope": {"sku_ids": [202]}},
            ),
            task("全店有哪些键盘布局", "task_2", tool_name="catalog_facets"),
        ],
        memory={"catalog": {"candidate_sku_ids": [101, 202]}},
    )
    assert "planning_error" not in state and len(planner.messages) == 1
    tasks = state["execution_plan"]["tasks"]
    assert all(t["depends_on"] == [] and t["answer_role"] == "user_facing" for t in tasks)
    assert tasks[1]["arguments"] == {"query": "全店有哪些键盘布局"}
    assert all(item["status"] == "ready" for item in state["task_status"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["query", "source_text", "rewritten_query", "missing_information"],
)
@pytest.mark.parametrize("persistent", [False, True])
async def test_blank_goal_content_is_repaired_before_handoff(field, persistent):
    message = "查询 键盘"
    valid_goal = goal(message)
    if field == "missing_information":
        valid_goal.update(disposition="clarification", missing_information=["商品对象"])
    valid = {"rewritten_query": message, "goals": [valid_goal]}
    bad = deepcopy(valid)
    if field == "rewritten_query":
        bad[field] = " \n\t"
    else:
        bad["goals"][0][field] = ["\u3000\t"] if field == "missing_information" else " "
    invalid = output("route_request", **bad)
    runtime, state, router, planner, _ = await plan_request(
        message, [valid_goal], [task(message)],
        router_outputs=[invalid, invalid if persistent else output("route_request", **valid)],
    )
    assert len(router.messages) == 2
    feedback = json.loads(router.messages[1][-1].content)["validation_feedback"]
    assert field in feedback["errors"][0]["path"]
    if persistent:
        assert "planning_error" in state and "execution_plan" not in state
        assert planner.messages == [] and runtime._dispatch_route(state) == "answer"
    else:
        assert "planning_error" not in state
        assert state["goal_plan"]["goals"][0]["query"] == message
        assert len(planner.messages) == (0 if field == "missing_information" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["llm1", "llm2"])
@pytest.mark.parametrize("persistent", [False, True])
async def test_invalid_json_repair_uses_formal_nodes_and_shared_budget(stage, persistent):
    query = "推荐两款300元以内白色蓝牙键盘，不要罗技"
    name = "route_request" if stage == "llm1" else "submit_execution_plan"
    invalid = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": name, "args": '{"broken":', "id": "bad", "error": "incomplete JSON"},
        ],
    )
    valid = (
        output(name, rewritten_query=query, goals=[goal(query)])
        if stage == "llm1"
        else output(name, tasks=[task(query, arguments={"query": query, "limit": 2})])
    )
    runtime, state, router, planner, frames = await plan_request(
        query,
        [goal(query)],
        [task(query, arguments={"query": query, "limit": 2})],
        **{
            "router_outputs" if stage == "llm1" else "planner_outputs": [
                invalid,
                invalid if persistent else valid,
            ]
        },
    )
    model = router if stage == "llm1" else planner
    assert len(model.messages) == 2
    repair = json.loads(model.messages[1][-1].content)
    assert repair["previous_output"]["invalid_tool_calls"][0]["args"] == '{"broken":'
    assert repair["validation_feedback"]["errors"][0]["code"] == "invalid_json"
    assert query in model.messages[1][1].content
    if persistent:
        assert "planning_error" in state and "execution_plan" not in state
        if stage == "llm1":
            assert planner.messages == [] and runtime._dispatch_route(state) == "answer"
        else:
            assert len(router.messages) == 1 and runtime._dispatch_planned(state) == "answer"
    else:
        assert "planning_error" not in state
        assert state["execution_plan"]["tasks"][0]["arguments"] == {"query": query, "limit": 2}
        assert (
            frames["request_router"]["planning_deadline_monotonic"]
            == (frames["plan_tools"]["planning_deadline_monotonic"])
        )


@pytest.mark.asyncio
async def test_network_retry_and_repair_do_not_bypass_injected_policy():
    query = "推荐无线鼠标"
    good = output("submit_execution_plan", tasks=[task(query)])
    bad = output("submit_execution_plan", tasks=[task(query, goal_id="goal_99")])
    runtime, state, router, planner, _ = await plan_request(
        query,
        [goal(query)],
        [],
        planner_outputs=[httpx.ConnectError("scripted"), bad, good],
        policy=replace(PLANNING_CALL_POLICY, retry_delay_seconds=0, max_attempts=2),
    )
    assert "planning_error" in state and "execution_plan" not in state
    assert len(router.messages) == 1 and len(planner.messages) == 2
    assert planner.messages[0] == planner.messages[1]
    assert planner.outputs == [good]
    assert state["task_planner_call_count"] == 2 and runtime._dispatch_planned(state) == "answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_reason", [None, "", " \n\t"])
@pytest.mark.parametrize("persistent", [False, True])
async def test_missing_goal_is_repaired_while_explicit_blocking_preserves_reason(
    invalid_reason, persistent,
):
    query = "推荐无线鼠标，查询最近订单"
    blocked = {"goal_id": "goal_2", "blocked_reason": "缺少完成该目标所需的条件"}
    invalid = output(
        "submit_execution_plan", tasks=[task("推荐无线鼠标")],
        blocked_goals=(
            [] if invalid_reason is None else [{**blocked, "blocked_reason": invalid_reason}]
        ),
    )
    corrected = output(
        "submit_execution_plan", tasks=[task("推荐无线鼠标")], blocked_goals=[blocked],
    )
    runtime, state, router, planner, _ = await plan_request(
        query,
        [goal("推荐无线鼠标"), goal("查询最近订单", "goal_2")],
        [],
        planner_outputs=[invalid, invalid if persistent else corrected],
    )
    assert len(router.messages) == 1 and len(planner.messages) == 2
    feedback = json.loads(planner.messages[1][-1].content)["validation_feedback"]
    if invalid_reason is not None:
        assert feedback["errors"][0]["path"] == ["blocked_goals", 0, "blocked_reason"]
    if persistent:
        assert "planning_error" in state and "execution_plan" not in state
        assert runtime._dispatch_planned(state) == "answer"
        return
    assert "planning_error" not in state
    assert runtime._dispatch_planned(state) == "execute"
    assert state["execution_plan"]["tasks"][0]["goal_id"] == "goal_1"
    assert state["execution_plan"]["blocked_goals"] == [blocked]
    assert state["route_plan"]["subqueries"][1]["query"] == "查询最近订单"
    assert state["route_plan"]["subqueries"][1]["tasks"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,valid", [("比较已找到的两款", False), ("比较 SKU 101", True), ("比较 SKU 202", False)]
)
async def test_known_part_of_mixed_context_and_future_binding_is_checked(query, valid):
    message = "比较刚才两款中的第一款与新找到的键盘"
    first = task("找一款键盘", answer_role="internal")
    second = task(
        query,
        "task_2",
        tool_name="catalog_search",
        arguments={"query": query},
        depends_on=["task_1"],
        bindings=[
            {"source": "context", "ref": "product_1", "kind": "product"},
            {"source": "task_output", "ref": "task_1", "kind": "product", "positions": [1]},
        ],
    )
    plan = output("submit_execution_plan", tasks=[first, second])
    _, state, _, planner, _ = await plan_request(
        message,
        [
            goal(
                "比较 SKU 101 与 SKU 202 中的第一款与新找到的键盘",
                source_text=message,

                context_refs=["product_1", "product_2"],
            )
        ],
        [],
        memory={"catalog": {"candidate_sku_ids": [101, 202]}},
        planner_outputs=[plan] if valid else [plan, plan],
    )
    if valid:
        assert "planning_error" not in state and len(planner.messages) == 1
        assert state["execution_plan"]["tasks"][1]["arguments"] == {
            "query": query,
        }
        assert state["task_artifacts"] == {}
    else:
        assert "planning_error" in state and "execution_plan" not in state
        assert len(planner.messages) == 2


@pytest.mark.asyncio
async def test_historical_order_goals_bind_separately_without_false_dependency():
    message = "查刚才前两笔订单的物流"
    goals = [
        goal(
            f"查订单{identity}的物流",
            f"goal_{index}",
            source_text=message,
            context_refs=[f"order_{index}"],
        )
        for index, identity in enumerate([88001, 88002], 1)
    ]
    tasks = [
        task(
            f"查订单{identity}的物流",
            f"task_{index}",
            goal_id=f"goal_{index}",
            tool_name="order_lookup",
            arguments={"query": f"查订单{identity}的物流", "order_id": identity},
            bindings=[
                {"source": "context", "ref": f"order_{index}", "kind": "order"},
            ],
        )
        for index, identity in enumerate([88001, 88002], 1)
    ]
    _, state, _, planner, _ = await plan_request(
        message,
        goals,
        tasks,
        memory={"order": {"candidate_order_ids": [88001, 88002]}},
    )
    assert "planning_error" not in state and len(planner.messages) == 1
    delivered = [ToolTask.model_validate(t) for t in state["execution_plan"]["tasks"]]
    assert [bind_arguments(state, t, t.arguments)["order_id"] for t in delivered] == [88001, 88002]
    assert all(t.depends_on == [] for t in delivered)
    assert all(bind_arguments(state, t, t.arguments) == t.arguments for t in delivered)
    assert state["user_id"] == 7


@pytest.mark.asyncio
async def test_natural_product_name_does_not_require_or_invent_id():
    query = "查 Logitech MX Master 3S 的价格和库存"
    _, state, _, planner, _ = await plan_request(query, [goal(query)], [task(query)])
    assert "planning_error" not in state and len(planner.messages) == 1
    delivered = state["execution_plan"]["tasks"][0]
    assert delivered["arguments"] == {"query": query}
    assert delivered["bindings"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,query", [
        ("catalog_search", "找一款白色键盘"),
        ("catalog_facets", "键盘有哪些布局"),
        ("policy_search", "退货有什么条件"),
        ("knowledge_search", "机械轴体有什么区别"),
        ("order_lookup", "查最近两笔订单"),
    ],
)
@pytest.mark.parametrize("persistent", [False, True])
async def test_blank_tool_query_is_repaired_before_delivery(tool, query, persistent):
    invalid = output("submit_execution_plan", tasks=[
        task(query, tool_name=tool, arguments={"query": " \n\u3000"}),
    ])
    corrected = output("submit_execution_plan", tasks=[task(query, tool_name=tool)])
    _, state, router, planner, _ = await plan_request(
        query, [goal(query)], [], planner_outputs=[invalid, invalid if persistent else corrected],
    )
    assert len(router.messages) == 1 and len(planner.messages) == 2
    feedback = json.loads(planner.messages[1][-1].content)["validation_feedback"]
    assert any("query" in error["path"] for error in feedback["errors"])
    if persistent:
        assert "planning_error" in state and "execution_plan" not in state
    else:
        assert "planning_error" not in state
        assert state["execution_plan"]["tasks"][0]["arguments"] == {"query": query}


@pytest.mark.asyncio
async def test_future_order_binding_still_requires_task_query():
    query = "列出最近两笔订单，再查第二笔物流"
    first = task("列出最近两笔订单", tool_name="order_lookup")
    second = task(
        "查第二笔物流", "task_2", tool_name="order_lookup", arguments={},
        depends_on=["task_1"],
        bindings=[{"source": "task_output", "ref": "task_1", "kind": "order", "positions": [2]}],
    )
    _, state, _, planner, _ = await plan_request(
        query, [goal(query)], [], planner_outputs=[
            output("submit_execution_plan", tasks=[first, second]),
            output("submit_execution_plan", tasks=[
                first, {**second, "arguments": {"query": "查第二笔物流"}},
            ]),
        ],
    )
    assert "planning_error" not in state and len(planner.messages) == 2
    assert "query or an explicit order_id" in planner.messages[1][-1].content
    assert state["execution_plan"]["tasks"][1]["arguments"] == {"query": "查第二笔物流"}


@pytest.mark.asyncio
@pytest.mark.parametrize("returned,total,positions,expected", [
    (2, 2, [2], 102), (2, 7, [2], 102), (1, 1, [1], 101), (1, 1, [], 101),
    (1, 1, [2], None), (2, 2, [], None), (0, 0, [1], None),
])
@pytest.mark.parametrize("analysis_query", ["最近两笔订单明细", "我买过雷蛇鼠标吗"])
async def test_order_analysis_objects_reach_dependent_tool(
    monkeypatch, returned, total, positions, expected, analysis_query,
):
    from datetime import datetime

    from app.agent.execution import execute_tool_wave
    from app.repositories.orders import OrderSearchPage
    from app.schemas.order import OrderCard
    from app.tools.contracts import BoundTool, RegistryToolExecutor, ToolCatalog
    from app.tools.orders import OrderToolService

    query = analysis_query + "，再查结果中的订单物流"
    followup = "查询选中订单的物流"
    runtime, state, _, planner, _ = await plan_request(query, [goal(query)], [
        task(analysis_query, tool_name="order_lookup", answer_role="internal",
             arguments={"query": analysis_query, "limit": 2}),
        task(followup, "task_2", tool_name="order_lookup", depends_on=["task_1"],
             bindings=[{"source": "task_output", "ref": "task_1", "kind": "order",
                        "positions": positions}]),
    ])
    assert "planning_error" not in state and len(planner.messages) == 1
    orders = [OrderCard(
        id=i, status=3, status_label="已发货", pay_amount=100,
        created_at=datetime(2026, 10, 1), items=[], logistics=None,
    ) for i in (101, 102)][:returned]
    lookups, audit = [], []

    class Repository:
        def __init__(self, session):
            pass

        async def list_recent_orders_page(self, user_id, *, limit, offset, filters=None):
            assert (user_id, limit, offset) == (7, 2, 0)
            return OrderSearchPage(orders=orders, total_count=total, offset=0)

        async def get_order(self, user_id, order_id):
            lookups.append((user_id, order_id))
            return next(o for o in orders if o.id == order_id)

    class Audit:
        def __init__(self, session):
            pass

        async def add_tool_call(self, *args):
            audit.append(args)

    monkeypatch.setattr("app.tools.orders.OrderRepository", Repository)
    from app.schemas.order import OrderFilters
    from app.tools.orders import OrderQueryPlan

    class Planner:
        async def plan(self, request):
            match = request.query == "我买过雷蛇鼠标吗"
            return OrderQueryPlan(
                selection="all" if match else "recent",
                operation="match_orders" if match else "detail",
                requested_fields=["purchase_history_match" if match else "order_detail"],
                filters=OrderFilters(product_terms=["雷蛇", "鼠标"] if match else []),
            )

    contract = runtime.contract_provider.get_contract("order_lookup")
    runtime.tool_executor = RegistryToolExecutor(
        None, runtime.settings,
        catalog=ToolCatalog([
            BoundTool(contract, OrderToolService(None, planner=Planner()).lookup),
        ]),
    )
    await runtime._orchestrate(state)
    await execute_tool_wave(runtime, state, repository_factory=Audit,
                            stream_writer_factory=lambda: lambda event: None)
    await runtime._normalize_tool_results(state)
    await runtime._extract_task_artifacts(state)
    await runtime._update_subquery_ledger(state)
    if not returned:
        assert state["task_status"]["task_2"]["status"] == "blocked"
        assert not lookups and len(audit) == 1
        return
    artifact = state["task_artifacts"]["task_1"]
    assert artifact["value"]["result_type"] == "order_analysis"
    assert artifact["value"]["is_exhaustive"] is (returned == total)
    assert [o["id"] for o in artifact["value"]["analysis_orders"]] == [o.id for o in orders]
    await runtime._orchestrate(state)
    await execute_tool_wave(runtime, state, repository_factory=Audit,
                            stream_writer_factory=lambda: lambda event: None)
    execution = state["tool_results"][-1]["execution"]
    if expected is None:
        assert lookups == []
        assert execution["ok"] is False and execution["error"]["code"] == "invalid_input"
    else:
        assert lookups == [(7, expected)]
        assert execution["ok"] is True and execution["output"]["order"]["id"] == expected
        assert state["tool_waves"][-1]["calls"][0]["arguments"] == {
            "query": followup, "order_id": expected,
        }


@pytest.mark.asyncio
async def test_multiple_explicit_orders_require_separate_complete_tasks():
    query = "列出最近的订单101和订单102"
    invalid = output("submit_execution_plan", tasks=[task(query, tool_name="order_lookup")])
    tasks = [task(
        f"查订单{order_id}", f"task_{index}", tool_name="order_lookup",
        arguments={"order_id": order_id},
    ) for index, order_id in enumerate((101, 102), 1)]
    _, state, _, planner, _ = await plan_request(
        query, [goal(query)], [],
        planner_outputs=[invalid, output("submit_execution_plan", tasks=tasks)],
    )
    assert len(planner.messages) == 2
    assert "planning_error" not in state
    assert [t["arguments"] for t in state["execution_plan"]["tasks"]] == [
        {"order_id": 101}, {"order_id": 102},
    ]


@pytest.mark.asyncio
async def test_retired_comparison_tool_cannot_reach_execution():
    query = "比较两款键盘"
    bad = output("submit_execution_plan", tasks=[task(query, tool_name="catalog_compare")])
    _, state, _, planner, _ = await plan_request(
        query, [goal(query)], [], planner_outputs=[bad, bad],
    )
    assert len(planner.messages) == 2
    assert "planning_error" in state and "execution_plan" not in state
    assert state.get("tool_wave_count", 0) == 0
