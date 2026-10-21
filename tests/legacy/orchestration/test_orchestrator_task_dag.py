from typing import Any, cast

import pytest
from app.agent.legacy.artifacts import (
    bound_task_catalog_targets,
    bound_task_order_id,
    extract_wave_artifacts,
    initialize_task_runtime,
    refresh_task_status,
)
from app.agent.legacy.capabilities import decision_from_route_capabilities
from app.agent.legacy.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.legacy.execution import _runtime_context_for_call
from app.agent.legacy.outcomes import build_subquery_ledger
from app.agent.legacy.routing import RequestRoutePlan, tool_planning_subqueries
from app.agent.legacy.runtime import AgentRuntime
from app.agent.legacy.state import AgentState
from app.agent.legacy.tool_loop import _find_reusable_tool_result
from app.core.config import Settings
from pydantic import ValidationError

from tests.support.planning_harness import goal, plan_request, task


def _bound_targets(
    state: AgentState,
    plan: RequestRoutePlan,
    task_id: str,
) -> list[dict[str, Any]]:
    task = next(item for item in tool_planning_subqueries(plan) if item.id == task_id)
    return bound_task_catalog_targets(state, task)


def _plan() -> RequestRoutePlan:
    return RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前商品和销量第二的键盘，再推荐一个鼠标",
            "subqueries": [
                {
                    "id": "sq_1",
                    "query": "查询键盘 SPU 销量排行第二的商品",
                    "disposition": "tool_planning",
                    "reason_code": "discover_ranked_keyboard",
                    "capability": "catalog_search",
                    "produces": "ranked_product",
                    "result_selector": {
                        "type": "sales_rank",
                        "rank": 2,
                        "scope": "spu",
                    },
                },
                {
                    "id": "sq_2",
                    "query": "查看键盘销量第二名的规格",
                    "disposition": "tool_planning",
                    "reason_code": "lookup_ranked_product",
                    "capability": "catalog_search",
                    "depends_on": ["sq_1"],
                    "input_requirements": [
                        {
                            "name": "right_product",
                            "source": "task_output",
                            "task_id": "sq_1",
                        },
                    ],
                    "produces": "products",
                },
                {
                    "id": "sq_3",
                    "query": "推荐一个鼠标",
                    "disposition": "tool_planning",
                    "reason_code": "recommend_mouse",
                    "capability": "catalog_search",
                    "produces": "products",
                },
            ],
        }
    )


def _product(
    sku_id: int,
    spu_id: int,
    *,
    sales_count: int,
    sku_sales_count: int,
) -> dict[str, Any]:
    return {
        "sku_id": sku_id,
        "spu_id": spu_id,
        "title": f"Keyboard {sku_id}",
        "brand": "Wooting",
        "category": "keyboard",
        "price": "399.00",
        "stock": 10,
        "sales_count": sales_count,
        "sku_sales_count": sku_sales_count,
        "specs": {},
    }


def _successful_result(
    call_id: str,
    name: str,
    products: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "tool_call_id": call_id,
        "name": name,
        "execution": {
            "tool_name": name,
            "ok": True,
            "output": {"result_type": "products", "products": products},
            "error": None,
        },
    }


def _state_after_first_wave() -> AgentState:
    plan = _plan()
    keyboard_products = [
        _product(701, 70, sales_count=9000, sku_sales_count=900),
        _product(702, 70, sales_count=9000, sku_sales_count=800),
        _product(703, 70, sales_count=9000, sku_sales_count=700),
        _product(711, 71, sales_count=8000, sku_sales_count=600),
        _product(712, 71, sales_count=8000, sku_sales_count=500),
        _product(721, 72, sales_count=7000, sku_sales_count=400),
    ]
    mouse_products = [_product(801, 80, sales_count=6000, sku_sales_count=600)]
    wave = {
        "wave": 1,
        "calls": [
            {
                "id": "router_sq_1_catalog_search",
                "name": "catalog_search",
                "arguments": {
                    "query": "查询键盘 SPU 销量排行第二的商品",
                    "limit": 2,
                },
                "subquery": "sq_1",
                "canonical_query": "查询键盘 SPU 销量排行第二的商品",
                "tool_query": "查询键盘 SPU 销量排行第二的商品",
            },
            {
                "id": "router_sq_3_catalog_search",
                "name": "catalog_search",
                "arguments": {"query": "推荐一个鼠标", "limit": 3},
                "subquery": "sq_3",
                "canonical_query": "推荐一个鼠标",
                "tool_query": "推荐一个鼠标",
            },
        ],
        "results": [
            _successful_result(
                "router_sq_1_catalog_search", "catalog_search", keyboard_products
            ),
            _successful_result(
                "router_sq_3_catalog_search", "catalog_search", mouse_products
            ),
        ],
    }
    return cast(
        AgentState,
        {
            "message": "那跟销量第二的比呢？",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory": {
                "catalog": {
                    "referenced_sku_id": 757,
                    "candidate_sku_ids": [757],
                }
            },
            "tool_waves": [wave],
            "tool_results": wave["results"],
            "tool_wave_count": 1,
            "subquery_ledger": [
                entry.model_dump(mode="json") for entry in build_subquery_ledger([wave])
            ],
            "orchestrator_call_count": 0,
        },
    )


def test_task_graph_rejects_cycles() -> None:
    payload = _plan().model_dump(mode="json")
    payload["subqueries"][0]["tasks"][0]["depends_on"] = ["sq_2"]

    with pytest.raises(ValidationError, match="acyclic"):
        RequestRoutePlan.model_validate(payload)


def test_ready_scheduler_parallelizes_independent_root_tasks() -> None:
    decision = decision_from_route_capabilities(_plan(), {})

    assert decision is not None
    assert [call.subquery for call in decision.tool_calls] == ["sq_1", "sq_3"]
    assert decision.tool_calls[0].arguments == {"limit": 3}


def test_tool_result_reuse_is_scoped_to_the_routed_task() -> None:
    previous_result = {
        "tool_call_id": "search-a",
        "name": "catalog_search",
        "execution": {
            "tool_name": "catalog_search",
            "ok": True,
            "output": {"result_type": "products", "products": [{"sku_id": 101}]},
            "error": None,
        },
    }
    state = cast(
        AgentState,
        {
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "search-a",
                            "name": "catalog_search",
                            "arguments": {"query": "查询这个系列的版本", "limit": 3},
                            "subquery": "task_a",
                        }
                    ],
                    "results": [previous_result],
                }
            ]
        },
    )
    same_arguments = {"query": "查询这个系列的版本", "limit": 3}

    assert (
        _find_reusable_tool_result(
            state,
            PlannedToolCall(
                id="search-b",
                name="catalog_search",
                arguments=same_arguments,
                subquery="task_b",
            ),
        )
        is None
    )
    assert (
        _find_reusable_tool_result(
            state,
            PlannedToolCall(
                id="search-a-retry",
                name="catalog_search",
                arguments=same_arguments,
                subquery="task_a",
            ),
        )
        == previous_result
    )


def test_ranked_search_preparation_stays_within_public_tool_contract() -> None:
    plan = _plan()
    decision = decision_from_route_capabilities(plan, {})
    assert decision is not None

    state = cast(
        AgentState,
        {
            "message": "对比一下这个和销量第二的有什么区别",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory": {
                "catalog": {
                    "referenced_sku_id": 757,
                    "candidate_sku_ids": [757],
                }
            },
        },
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))

    effective, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])

    assert effective.arguments == {
        "query": "查询键盘 SPU 销量排行第二的商品",
        "limit": 3,
    }


def _runtime_state(
    plan: RequestRoutePlan,
    message: str,
    *,
    referenced_sku_id: int | None = None,
) -> AgentState:
    catalog = (
        {
            "referenced_sku_id": referenced_sku_id,
            "candidate_sku_ids": [referenced_sku_id],
        }
        if referenced_sku_id is not None
        else {}
    )
    state = cast(
        AgentState,
        {
            "message": message,
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {"catalog": catalog},
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )
    initialize_task_runtime(state)
    return state


def _apply_wave(
    state: AgentState,
    calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    wave = {
        "wave": len(state["tool_waves"]) + 1,
        "calls": calls,
        "results": results,
    }
    state["tool_waves"].append(wave)
    state["tool_results"].extend(results)
    state["tool_wave_count"] = wave["wave"]
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        item.model_dump(mode="json")
        for item in build_subquery_ledger(state["tool_waves"])
    ]
    refresh_task_status(state)


def test_depends_on_only_selects_order_candidate_for_detail_wave() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "查看第二个候选订单的物流详情",
            "subqueries": [
                {
                    "id": "goal_22",
                    "query": "查看第二个候选订单的物流详情",
                    "disposition": "tool_planning",
                    "reason_code": "select_candidate_then_detail",
                    "tasks": [
                        {
                            "id": "task_25",
                            "goal_id": "goal_22",
                            "canonical_query": "查询最近订单候选",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "result",
                            "answer_role": "internal",
                            "capability": "planner_required",
                        },
                        {
                            "id": "task_26",
                            "goal_id": "goal_22",
                            "canonical_query": "查询第二个候选订单的物流详情",
                            "depends_on": ["task_25"],
                            "input_requirements": [],
                            "produces": "result",
                            "answer_role": "user_facing",
                            "capability": "planner_required",
                        },
                    ],
                }
            ],
        }
    )
    state = _runtime_state(plan, "查看第二个候选订单的物流详情")
    _apply_wave(
        state,
        [
            {
                "id": "recent_orders",
                "name": "order_lookup",
                "arguments": {"query": "查询最近订单候选", "limit": 5},
                "subquery": "task_25",
            }
        ],
        [
            {
                "tool_call_id": "recent_orders",
                "name": "order_lookup",
                "execution": {
                    "tool_name": "order_lookup",
                    "ok": True,
                    "output": {
                        "result_type": "order_candidates",
                        "candidates": [{"id": 4321}, {"id": 4322}],
                        "focus_order_ids": [4321, 4322],
                    },
                    "error": None,
                },
            }
        ],
    )

    task = tool_planning_subqueries(plan)[1]
    assert bound_task_order_id(state, task) == 4322
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    decision = OrchestratorDecision(
        type="tool_calls",
        tool_calls=[
            PlannedToolCall(
                id="order_detail",
                name="order_lookup",
                arguments={"query": "查询第二个候选订单的物流详情", "limit": 5},
                subquery="task_26",
            )
        ],
    )
    validated = runtime._validate_decision_budget(state, decision, call_count=1)
    assert validated.type == "tool_calls"
    prepared, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert prepared.arguments["order_id"] == 4322


@pytest.mark.asyncio
async def test_context_product_accepts_one_spu_with_multiple_sku_variants() -> None:
    query = "哪个版本最便宜"
    resolved = "查询SPU 63的各版本价格"
    runtime, state, _, _, _ = await plan_request(
        query,
        [goal(resolved, source_text=query, context_refs=["product_1"])],
        [task(resolved, arguments={"query": resolved, "scope": {"spu_ids": [63]}, "limit": 3},
              bindings=[{"source": "context", "ref": "product_1", "kind": "product"}])],
        memory={"catalog": {"query_plan": {"selection_scope": "spu"},
                            "candidate_spu_ids": [63], "candidate_sku_ids": [745, 746, 747]}},
    )
    assert "planning_error" not in state
    assert state["task_status"]["task_1"]["status"] == "ready"
    assert state["context_objects"]["product_1"]["ids"] == [63]
    await runtime._orchestrate(state)
    call = PlannedToolCall.model_validate(state["decision"]["tool_calls"][0])
    prepared, _ = runtime._prepare_tool_call(state, call)
    assert prepared.arguments == {
        "query": resolved, "scope": {"sku_ids": [], "spu_ids": [63]}, "limit": 3,
    }
    assert _runtime_context_for_call(state, prepared) == {"user_id": 7}

    # Ambiguity belongs to LLM1; Runtime must preserve its clarification, not guess a model.
    _, ambiguous, _, planner, _ = await plan_request(
        query,
        [goal(query, disposition="clarification", missing_information=["要比较哪个型号的版本"])],
        [],
        memory={"catalog": {"query_plan": {"selection_scope": "spu"},
                            "candidate_spu_ids": [63, 72], "candidate_sku_ids": [745, 801]}},
    )
    assert ambiguous["route_plan"]["subqueries"][0]["disposition"] == "clarification"
    assert ambiguous["route_plan"]["subqueries"][0]["tasks"] == []
    assert planner.messages == [] and ambiguous["tool_results"] == []


def test_order_artifact_binds_unique_candidate_while_catalog_goal_stays_independent() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "查询最近订单详情，再推荐一个鼠标",
            "subqueries": [
                {
                    "id": "goal_10",
                    "query": "查询最近订单详情",
                    "disposition": "tool_planning",
                    "reason_code": "latest_order_detail",
                    "tasks": [
                        {
                            "id": "task_10",
                            "goal_id": "goal_10",
                            "canonical_query": "查询最近订单候选",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "order",
                            "answer_role": "internal",
                            "capability": "order_lookup",
                        },
                        {
                            "id": "task_11",
                            "goal_id": "goal_10",
                            "canonical_query": "查询选中订单的详情",
                            "depends_on": ["task_10"],
                            "input_requirements": [
                                {
                                    "name": "order_id",
                                    "source": "task_output",
                                    "task_id": "task_10",
                                }
                            ],
                            "produces": "order",
                            "answer_role": "user_facing",
                            "capability": "order_lookup",
                        },
                    ],
                },
                {
                    "id": "goal_11",
                    "query": "推荐一个鼠标",
                    "disposition": "tool_planning",
                    "reason_code": "recommend_mouse",
                    "tasks": [
                        {
                            "id": "task_12",
                            "goal_id": "goal_11",
                            "canonical_query": "推荐一个鼠标",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "products",
                            "answer_role": "user_facing",
                            "capability": "catalog_search",
                        }
                    ],
                },
            ],
        }
    )
    state = _runtime_state(plan, "查询最近订单详情，再推荐一个鼠标")
    first = decision_from_route_capabilities(plan, state)
    assert first is not None
    assert [(call.subquery, call.name) for call in first.tool_calls] == [
        ("task_10", "order_lookup"),
        ("task_12", "catalog_search"),
    ]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    calls = [
        runtime._prepare_tool_call(state, call)[0].model_dump(mode="json")
        for call in first.tool_calls
    ]
    order_candidates = {
        "tool_call_id": calls[0]["id"],
        "name": "order_lookup",
        "execution": {
            "tool_name": "order_lookup",
            "ok": True,
            "output": {
                "result_type": "order_candidates",
                "order": None,
                "candidates": [
                    {
                        "id": 4321,
                        "status": 2,
                        "status_label": "已发货",
                        "pay_amount": "399.00",
                        "created_at": "2026-07-20T12:00:00",
                        "item_count": 1,
                        "first_item_name": "鼠标",
                        "logistic_no": "SF123",
                    }
                ],
            },
            "error": None,
        },
    }
    _apply_wave(
        state,
        calls,
        [
            order_candidates,
            _successful_result(
                calls[1]["id"],
                "catalog_search",
                [_product(901, 90, sales_count=5000, sku_sales_count=500)],
            ),
        ],
    )

    assert state["task_status"]["task_11"]["status"] == "ready"
    second = decision_from_route_capabilities(plan, state)
    assert second is not None
    validated = runtime._validate_decision_budget(state, second, call_count=1)
    detail_call, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert detail_call.subquery == "task_11"
    assert detail_call.arguments["order_id"] == 4321
    assert detail_call.arguments["query"] == "查询选中订单的详情"
