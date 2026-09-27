from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.agent.answer_context import build_answer_context
from app.agent.artifacts import (
    bound_task_catalog_targets,
    bound_task_order_id,
    extract_wave_artifacts,
    initialize_task_runtime,
    refresh_task_status,
)
from app.agent.capabilities import decision_from_route_capabilities
from app.agent.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.execution import _runtime_context_for_call
from app.agent.graph import AgentRuntime, _followup_tool_call_allowed
from app.agent.outcomes import build_subquery_ledger, validate_terminal_decision
from app.agent.route_runtime import _reuse_comparison_context
from app.agent.routing import RequestRoutePlan, tool_planning_subqueries
from app.agent.state import AgentState
from app.agent.tool_loop import _find_reusable_tool_result
from app.core.config import Settings


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
                    "query": "比较当前商品与键盘销量第二名的区别",
                    "disposition": "tool_planning",
                    "reason_code": "compare_products",
                    "capability": "catalog_compare",
                    "depends_on": ["sq_1"],
                    "input_requirements": [
                        {"name": "left_product", "source": "context_product"},
                        {
                            "name": "right_product",
                            "source": "task_output",
                            "task_id": "sq_1",
                        },
                    ],
                    "produces": "comparison",
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


def test_spu_compare_task_binds_series_ids_from_context_and_ranked_artifact() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前键盘系列和销量第二的键盘系列",
            "subqueries": [
                {
                    "id": "goal_1",
                    "query": "对比当前键盘系列和销量第二的键盘系列",
                    "disposition": "tool_planning",
                    "reason_code": "compare_ranked_series",
                    "tasks": [
                        {
                            "id": "task_1",
                            "goal_id": "goal_1",
                            "canonical_query": "查询键盘 SPU 销量第二的商品",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                            "result_selector": {
                                "type": "sales_rank",
                                "rank": 2,
                                "scope": "spu",
                            },
                        },
                        {
                            "id": "task_2",
                            "goal_id": "goal_1",
                            "canonical_query": "比较两个键盘系列的全部在售版本",
                            "depends_on": ["task_1"],
                            "input_requirements": [
                                {"name": "left", "source": "context_product"},
                                {
                                    "name": "right",
                                    "source": "task_output",
                                    "task_id": "task_1",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                            "comparison_level": "spu",
                        },
                    ],
                }
            ],
        }
    )
    state = cast(
        AgentState,
        {
            "message": "这个和销量第二的比",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {
                "catalog": {
                    "referenced_spu_id": 75,
                    "referenced_sku_id": 757,
                    "candidate_spu_ids": [75],
                    "candidate_sku_ids": [757],
                }
            },
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )
    initialize_task_runtime(state)
    first_decision = decision_from_route_capabilities(plan, state)
    assert first_decision is not None
    assert [call.subquery for call in first_decision.tool_calls] == ["task_1"]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    first_call, _ = runtime._prepare_tool_call(state, first_decision.tool_calls[0])
    ranked_products = [
        _product(701, 70, sales_count=9000, sku_sales_count=900),
        _product(711, 71, sales_count=8000, sku_sales_count=600),
    ]
    first_wave = {
        "wave": 1,
        "calls": [first_call.model_dump(mode="json")],
        "results": [
            _successful_result(first_call.id, "catalog_search", ranked_products)
        ],
    }
    state["tool_waves"] = [first_wave]
    state["tool_results"] = list(first_wave["results"])
    state["tool_wave_count"] = 1
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        item.model_dump(mode="json") for item in build_subquery_ledger([first_wave])
    ]
    refresh_task_status(state)

    assert state["task_artifacts"]["task_1"]["value"]["selected_spu_ids"] == [71]
    assert state["task_status"]["task_2"]["status"] == "ready"
    second_decision = decision_from_route_capabilities(plan, state)
    assert second_decision is not None
    compare_call, _ = runtime._prepare_tool_call(state, second_decision.tool_calls[0])
    assert compare_call.arguments == {
        "query": "比较两个键盘系列的全部在售版本",
        "limit": 5,
    }
    assert _bound_targets(state, plan, "task_2") == [
        {
            "sku_id": 757,
            "spu_id": 75,
            "source": "working_memory_reference",
        },
        {
            "sku_id": 711,
            "spu_id": 71,
            "source": "current_turn_artifact",
        },
    ]


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


def test_two_parallel_catalog_chains_keep_wave_two_targets_isolated() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "分别比较两组键盘和鼠标",
            "subqueries": [
                {
                    "id": "goal_30",
                    "query": "分别比较两组键盘和鼠标",
                    "disposition": "tool_planning",
                    "reason_code": "compare_two_independent_pairs",
                    "tasks": [
                        {
                            "id": "task_301",
                            "goal_id": "goal_30",
                            "canonical_query": "查询销量第一的键盘 SPU",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                        },
                        {
                            "id": "task_302",
                            "goal_id": "goal_30",
                            "canonical_query": "查询销量第二的键盘 SPU",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                        },
                        {
                            "id": "task_303",
                            "goal_id": "goal_30",
                            "canonical_query": "比较这两个键盘系列",
                            "depends_on": ["task_301", "task_302"],
                            "input_requirements": [
                                {
                                    "name": "left",
                                    "source": "task_output",
                                    "task_id": "task_301",
                                },
                                {
                                    "name": "right",
                                    "source": "task_output",
                                    "task_id": "task_302",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        },
                        {
                            "id": "task_304",
                            "goal_id": "goal_30",
                            "canonical_query": "查询销量第一的鼠标 SPU",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                        },
                        {
                            "id": "task_305",
                            "goal_id": "goal_30",
                            "canonical_query": "查询销量第二的鼠标 SPU",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                        },
                        {
                            "id": "task_306",
                            "goal_id": "goal_30",
                            "canonical_query": "比较这两个鼠标系列",
                            "depends_on": ["task_304", "task_305"],
                            "input_requirements": [
                                {
                                    "name": "left",
                                    "source": "task_output",
                                    "task_id": "task_304",
                                },
                                {
                                    "name": "right",
                                    "source": "task_output",
                                    "task_id": "task_305",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        },
                    ],
                }
            ],
        }
    )
    state = _runtime_state(plan, "分别比较两组键盘和鼠标")
    state["user_id"] = 7
    first = decision_from_route_capabilities(plan, state)
    assert first is not None
    assert [call.subquery for call in first.tool_calls] == [
        "task_301",
        "task_302",
        "task_304",
        "task_305",
    ]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    prepared = [runtime._prepare_tool_call(state, call)[0] for call in first.tool_calls]
    products = [
        _product(101, 10, sales_count=4000, sku_sales_count=400),
        _product(201, 20, sales_count=3000, sku_sales_count=300),
        _product(301, 30, sales_count=2000, sku_sales_count=200),
        _product(401, 40, sales_count=1000, sku_sales_count=100),
    ]
    _apply_wave(
        state,
        [call.model_dump(mode="json") for call in prepared],
        [
            _successful_result(call.id, "catalog_search", [product])
            for call, product in zip(prepared, products, strict=True)
        ],
    )

    second = decision_from_route_capabilities(plan, state)
    assert second is not None
    assert [call.subquery for call in second.tool_calls] == [
        "task_303",
        "task_306",
    ]
    prepared_second = [
        runtime._prepare_tool_call(state, call)[0] for call in second.tool_calls
    ]
    assert [call.arguments for call in prepared_second] == [
        {"query": "比较这两个键盘系列", "limit": 5},
        {"query": "比较这两个鼠标系列", "limit": 5},
    ]
    catalog_contexts = [
        context["catalog_context"]
        for context in (
            _runtime_context_for_call(state, prepared_second[0]),
            _runtime_context_for_call(state, prepared_second[1]),
        )
    ]
    assert [
        [
            artifact["selected_sku_ids"]
            for artifact in context["dependency_artifacts"]
        ]
        for context in catalog_contexts
    ] == [[[101], [201]], [[301], [401]]]
    assert [
        [
            artifact["selected_spu_ids"]
            for artifact in context["dependency_artifacts"]
        ]
        for context in catalog_contexts
    ] == [[[10], [20]], [[30], [40]]]


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


def test_next_wave_uses_ready_compare_task_without_reclassifying_raw_message() -> None:
    state = _state_after_first_wave()
    decision = decision_from_route_capabilities(_plan(), state)

    assert decision is not None
    assert [call.subquery for call in decision.tool_calls] == ["sq_2"]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    validated = runtime._validate_decision_budget(state, decision, call_count=0)
    assert validated.type == "tool_calls"

    effective, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert effective.arguments == {
        "query": "比较当前商品与键盘销量第二名的区别",
        "limit": 5,
    }
    assert _bound_targets(state, _plan(), "sq_2") == [
        {"sku_id": 757, "source": "working_memory_reference"},
        {
            "sku_id": 711,
            "spu_id": 71,
            "source": "current_turn_artifact",
        },
    ]
    assert effective.arguments["query"] == "比较当前商品与键盘销量第二名的区别"
    assert effective.canonical_query == "比较当前商品与键盘销量第二名的区别"
    assert _followup_tool_call_allowed(state, effective) is True


def test_structured_compare_binding_does_not_append_raw_message_candidates() -> None:
    state = _state_after_first_wave()
    state["message"] = "那跟第二个比呢？"
    state["working_memory"]["catalog"]["candidate_sku_ids"] = [757, 999]
    decision = decision_from_route_capabilities(_plan(), state)
    assert decision is not None

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    effective, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])

    assert effective.arguments == {
        "query": "比较当前商品与键盘销量第二名的区别",
        "limit": 5,
    }
    assert [item.get("sku_id") for item in _bound_targets(state, _plan(), "sq_2")] == [
        757,
        711,
    ]


def test_comparison_followup_binds_previous_pair_without_catalog_search() -> None:
    working_memory = {
        "catalog": {
            "comparison": {"sku_ids": [757, 745]},
            "candidate_display": [
                {"sku_id": 757, "title": "Wooting 曜石 K08 黑色标准版"},
                {"sku_id": 745, "title": "Wooting 青锋 K07 黑色标准版"},
            ],
        }
    }
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "比较已确认的两款键盘，判断哪个价格更低",
            "subqueries": [
                {
                    "id": "sq_1",
                    "query": "查询 Wooting 曜石 K08 黑色标准版（SKU ID: 757）的价格",
                    "disposition": "tool_planning",
                    "reason_code": "refresh_first_product",
                    "capability": "catalog_search",
                    "produces": "products",
                },
                {
                    "id": "sq_2",
                    "query": "查询 Wooting 青锋 K07 黑色标准版（SKU ID: 745）的价格",
                    "disposition": "tool_planning",
                    "reason_code": "refresh_second_product",
                    "capability": "catalog_search",
                    "produces": "products",
                },
                {
                    "id": "sq_3",
                    "query": "比较 Wooting 曜石 K08 和 Wooting 青锋 K07 的价格",
                    "disposition": "tool_planning",
                    "reason_code": "compare_prices",
                    "capability": "catalog_compare",
                    "depends_on": ["sq_1", "sq_2"],
                    "input_requirements": [
                        {"name": "first", "source": "task_output", "task_id": "sq_1"},
                        {"name": "second", "source": "task_output", "task_id": "sq_2"},
                    ],
                    "produces": "comparison",
                },
            ],
        }
    )
    plan = _reuse_comparison_context(plan, working_memory)

    assert len(plan.subqueries) == 1
    assert plan.subqueries[0].capability == "catalog_compare"
    assert plan.subqueries[0].input_requirements[0].source == "comparison_context"

    decision = decision_from_route_capabilities(plan, {})
    assert decision is not None
    assert [call.name for call in decision.tool_calls] == ["catalog_compare"]

    state = cast(
        AgentState,
        {
            "message": "哪个便宜",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory": working_memory,
        },
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    effective, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])

    assert effective.arguments == {
        "query": "比较 Wooting 曜石 K08 和 Wooting 青锋 K07 的价格",
        "limit": 5,
    }
    compare_task_id = tool_planning_subqueries(plan)[0].id
    assert _bound_targets(state, plan, compare_task_id) == [
        {"sku_id": 757, "source": "comparison_context"},
        {"sku_id": 745, "source": "comparison_context"},
    ]


def test_two_goals_execute_as_root_wave_then_dependent_wave() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前商品和销量第二的键盘，再推荐一个鼠标",
            "subqueries": [
                {
                    "id": "goal_1",
                    "query": "对比当前商品和销量第二的键盘",
                    "disposition": "tool_planning",
                    "reason_code": "compare_with_ranked_keyboard",
                    "tasks": [
                        {
                            "id": "task_1",
                            "goal_id": "goal_1",
                            "canonical_query": "查询键盘 SPU 销量排行第二的商品",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                            "result_selector": {
                                "type": "sales_rank",
                                "rank": 2,
                                "scope": "spu",
                            },
                        },
                        {
                            "id": "task_2",
                            "goal_id": "goal_1",
                            "canonical_query": "比较当前商品与键盘销量第二名的区别",
                            "depends_on": ["task_1"],
                            "input_requirements": [
                                {"name": "left", "source": "context_product"},
                                {
                                    "name": "right",
                                    "source": "task_output",
                                    "task_id": "task_1",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        },
                    ],
                },
                {
                    "id": "goal_2",
                    "query": "推荐一个鼠标",
                    "disposition": "tool_planning",
                    "reason_code": "recommend_mouse",
                    "tasks": [
                        {
                            "id": "task_3",
                            "goal_id": "goal_2",
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
    state = cast(
        AgentState,
        {
            "message": "对比这个和销量第二的键盘，再推荐一个鼠标",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {
                "catalog": {
                    "referenced_sku_id": 757,
                    "candidate_sku_ids": [757],
                }
            },
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )
    initialize_task_runtime(state)

    first_decision = decision_from_route_capabilities(plan, state)
    assert first_decision is not None
    assert [call.subquery for call in first_decision.tool_calls] == ["task_1", "task_3"]
    assert state["task_status"]["task_2"]["status"] == "pending"

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    first_calls = [
        runtime._prepare_tool_call(state, call)[0].model_dump(mode="json")
        for call in first_decision.tool_calls
    ]
    keyboard_products = [
        _product(701, 70, sales_count=9000, sku_sales_count=900),
        _product(711, 71, sales_count=8000, sku_sales_count=600),
    ]
    mouse_products = [_product(801, 80, sales_count=6000, sku_sales_count=600)]
    first_wave = {
        "wave": 1,
        "calls": first_calls,
        "results": [
            _successful_result(first_calls[0]["id"], "catalog_search", keyboard_products),
            _successful_result(first_calls[1]["id"], "catalog_search", mouse_products),
        ],
    }
    state["tool_waves"] = [first_wave]
    state["tool_results"] = list(first_wave["results"])
    state["tool_wave_count"] = 1
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        item.model_dump(mode="json") for item in build_subquery_ledger([first_wave])
    ]
    refresh_task_status(state)

    assert state["task_artifacts"]["task_1"]["value"]["selected_sku_ids"] == [711]
    assert state["task_status"]["task_1"]["status"] == "succeeded"
    assert state["task_status"]["task_2"]["status"] == "ready"
    assert state["task_status"]["task_3"]["status"] == "succeeded"

    second_decision = decision_from_route_capabilities(plan, state)
    assert second_decision is not None
    assert [call.subquery for call in second_decision.tool_calls] == ["task_2"]
    validated = runtime._validate_decision_budget(state, second_decision, call_count=1)
    assert validated.type == "tool_calls"
    compare_call, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert compare_call.arguments == {
        "query": "比较当前商品与键盘销量第二名的区别",
        "limit": 5,
    }
    assert [item.get("sku_id") for item in _bound_targets(state, plan, "task_2")] == [
        757,
        711,
    ]

    compare_result = {
        "tool_call_id": compare_call.id,
        "name": "catalog_compare",
        "execution": {
            "tool_name": "catalog_compare",
            "ok": True,
            "output": {
                "result_type": "comparison",
                "products": [
                    _product(757, 75, sales_count=7500, sku_sales_count=550),
                    keyboard_products[1],
                ],
                "comparison_fields": ["price", "sales_count"],
            },
            "error": None,
        },
    }
    second_wave = {
        "wave": 2,
        "calls": [compare_call.model_dump(mode="json")],
        "results": [compare_result],
    }
    state["tool_waves"].append(second_wave)
    state["tool_results"].append(compare_result)
    state["tool_wave_count"] = 2
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        item.model_dump(mode="json")
        for item in build_subquery_ledger(state["tool_waves"])
    ]
    refresh_task_status(state)

    assert state["task_status"]["task_2"]["status"] == "succeeded"
    terminal = OrchestratorDecision(
        type="grounded_response",
        response="比较结果与鼠标推荐均已完成。",
        control_action="finish_answer",
        used_tool_call_ids=[compare_call.id, first_calls[1]["id"]],
    )
    validation = validate_terminal_decision(
        terminal,
        state["subquery_ledger"],
        planned_subquery_ids=["task_2", "task_3"],
        resolved_task_ids=["task_1", "task_2", "task_3"],
        usable_artifact_tool_call_ids=[
            artifact["source_tool_call_id"]
            for artifact in state["task_artifacts"].values()
            if artifact["usable"]
        ],
    )
    assert validation.valid is True


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


def test_depends_on_only_passes_ranked_catalog_products_to_compare_wave() -> None:
    query = "把销量前两名游戏鼠标的重量和 DPI 对比一下"
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": query,
            "subqueries": [
                {
                    "id": "goal_20",
                    "query": query,
                    "disposition": "tool_planning",
                    "reason_code": "rank_then_compare",
                    "tasks": [
                        {
                            "id": "task_20",
                            "goal_id": "goal_20",
                            "canonical_query": "找出销量排名前两名的游戏鼠标",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "result",
                            "answer_role": "internal",
                            "capability": "planner_required",
                        },
                        {
                            "id": "task_21",
                            "goal_id": "goal_20",
                            "canonical_query": query,
                            "depends_on": ["task_20"],
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
    state = _runtime_state(plan, query)
    state["user_id"] = 1
    products = [
        _product(901, 90, sales_count=9000, sku_sales_count=900),
        _product(902, 91, sales_count=8000, sku_sales_count=800),
    ]
    _apply_wave(
        state,
        [
            {
                "id": "search_top_two",
                "name": "catalog_search",
                "arguments": {"query": "找出销量排名前两名的游戏鼠标", "limit": 3},
                "subquery": "task_20",
            }
        ],
        [_successful_result("search_top_two", "catalog_search", products)],
    )

    task = tool_planning_subqueries(plan)[1]
    assert state["task_status"]["task_21"]["status"] == "ready"
    assert bound_task_catalog_targets(state, task) == [
        {"sku_id": 901, "spu_id": 90, "source": "current_turn_artifact"},
        {"sku_id": 902, "spu_id": 91, "source": "current_turn_artifact"},
    ]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    decision = OrchestratorDecision(
        type="tool_calls",
        tool_calls=[
            PlannedToolCall(
                id="compare_top_two",
                name="catalog_compare",
                arguments={"query": query, "limit": 5},
                subquery="task_21",
            )
        ],
    )
    validated = runtime._validate_decision_budget(state, decision, call_count=1)
    assert validated.type == "tool_calls"
    prepared, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    catalog_context = _runtime_context_for_call(state, prepared)["catalog_context"]
    assert catalog_context["dependency_artifacts"][0]["task_id"] == "task_20"
    assert [item["sku_id"] for item in catalog_context["dependency_artifacts"][0]["products"]] == [
        901,
        902,
    ]


def test_dependent_catalog_task_reads_complete_artifact_without_working_memory() -> None:
    query = "和销量第二的鼠标对比一下"
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": query,
            "subqueries": [
                {
                    "id": "goal_23",
                    "query": query,
                    "disposition": "tool_planning",
                    "reason_code": "compare_current_with_ranked",
                    "tasks": [
                        {
                            "id": "task_27",
                            "goal_id": "goal_23",
                            "canonical_query": "查询当前商品以及销量第二的鼠标，返回两项商品结果",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "result",
                            "answer_role": "internal",
                            "capability": "planner_required",
                        },
                        {
                            "id": "task_28",
                            "goal_id": "goal_23",
                            "canonical_query": "仅比较 task_27 返回的两个商品",
                            "depends_on": ["task_27"],
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
    state = _runtime_state(plan, query)
    state["working_memory_snapshot"] = {
        "catalog": {
            "query_plan": {"selection_scope": "spu"},
            "candidate_sku_ids": [901],
            "candidate_spu_ids": [90],
            "candidate_display": [
                {
                    "sku_id": 901,
                    "spu_id": 90,
                    "title": "Current Mouse",
                    "entity_scope": "spu",
                }
            ],
        }
    }
    _apply_wave(
        state,
        [
            {
                "id": "search_second_mouse",
                "name": "catalog_search",
                "arguments": {
                    "query": "查询当前商品以及销量第二的鼠标，返回两项商品结果",
                    "limit": 3,
                },
                "subquery": "task_27",
            }
        ],
        [
            _successful_result(
                "search_second_mouse",
                "catalog_search",
                [
                    _product(901, 90, sales_count=9000, sku_sales_count=900),
                    _product(902, 91, sales_count=8000, sku_sales_count=800),
                ],
            )
        ],
    )

    task = tool_planning_subqueries(plan)[1]
    assert bound_task_catalog_targets(state, task) == [
        {"sku_id": 901, "spu_id": 90, "source": "current_turn_artifact"},
        {"sku_id": 902, "spu_id": 91, "source": "current_turn_artifact"},
    ]
    call = PlannedToolCall(
        id="compare_current_ranked",
        name="catalog_compare",
        arguments={"query": "仅比较 task_27 返回的两个商品", "limit": 5},
        subquery="task_28",
    )
    state["user_id"] = 1
    catalog_context = _runtime_context_for_call(state, call)["catalog_context"]
    assert catalog_context["memory"] == {}
    assert len(catalog_context["dependency_artifacts"][0]["products"]) == 2
    assert _followup_tool_call_allowed(state, call) is True


def test_depends_on_only_merges_two_catalog_search_artifacts_for_compare_wave() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "比较销量第一的键盘和销量第一的鼠标",
            "subqueries": [
                {
                    "id": "goal_21",
                    "query": "比较销量第一的键盘和销量第一的鼠标",
                    "disposition": "tool_planning",
                    "reason_code": "parallel_rank_then_compare",
                    "tasks": [
                        {
                            "id": task_id,
                            "goal_id": "goal_21",
                            "canonical_query": task_query,
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "result",
                            "answer_role": "internal",
                            "capability": "planner_required",
                        }
                        for task_id, task_query in (
                            ("task_22", "找出销量第一的键盘"),
                            ("task_23", "找出销量第一的鼠标"),
                        )
                    ]
                    + [
                        {
                            "id": "task_24",
                            "goal_id": "goal_21",
                            "canonical_query": "比较销量第一的键盘和销量第一的鼠标",
                            "depends_on": ["task_22", "task_23"],
                            "input_requirements": [],
                            "produces": "result",
                            "answer_role": "user_facing",
                            "capability": "planner_required",
                        }
                    ],
                }
            ],
        }
    )
    state = _runtime_state(plan, "比较销量第一的键盘和销量第一的鼠标")
    _apply_wave(
        state,
        [
            {
                "id": "search_keyboard",
                "name": "catalog_search",
                "arguments": {"query": "找出销量第一的键盘", "limit": 3},
                "subquery": "task_22",
            },
            {
                "id": "search_mouse",
                "name": "catalog_search",
                "arguments": {"query": "找出销量第一的鼠标", "limit": 3},
                "subquery": "task_23",
            },
        ],
        [
            _successful_result(
                "search_keyboard",
                "catalog_search",
                [_product(701, 70, sales_count=9000, sku_sales_count=900)],
            ),
            _successful_result(
                "search_mouse",
                "catalog_search",
                [_product(801, 80, sales_count=8000, sku_sales_count=800)],
            ),
        ],
    )

    task = tool_planning_subqueries(plan)[2]
    assert bound_task_catalog_targets(state, task) == [
        {"sku_id": 701, "spu_id": 70, "source": "current_turn_artifact"},
        {"sku_id": 801, "spu_id": 80, "source": "current_turn_artifact"},
    ]


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


def test_context_product_accepts_one_spu_with_multiple_sku_variants() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "查询这个键盘哪个版本最便宜",
            "subqueries": [
                {
                    "id": "goal_1",
                    "query": "查询这个键盘哪个版本最便宜",
                    "disposition": "tool_planning",
                    "reason_code": "rank_variants_in_current_series",
                    "tasks": [
                        {
                            "id": "task_1",
                            "goal_id": "goal_1",
                            "canonical_query": "查询这个键盘哪个版本最便宜",
                            "depends_on": [],
                            "input_requirements": [
                                {"name": "current_keyboard", "source": "context_product"}
                            ],
                            "produces": "ranked_product",
                            "answer_role": "user_facing",
                            "capability": "catalog_search",
                        }
                    ],
                }
            ],
        }
    )
    state = cast(
        AgentState,
        {
            "user_id": 7,
            "message": "哪个版本最便宜",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {
                "catalog": {
                    "candidate_sku_ids": [745, 746, 747],
                    "candidate_spu_ids": [63],
                }
            },
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )
    initialize_task_runtime(state)

    assert state["task_status"]["task_1"]["status"] == "ready"
    assert _bound_targets(state, plan, "task_1") == [
        {"source": "working_memory_reference", "spu_id": 63}
    ]

    decision = decision_from_route_capabilities(plan, state)
    assert decision is not None
    assert decision.tool_calls[0].arguments == {"limit": 3}
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    prepared, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])

    assert prepared.arguments == {
        "query": "查询这个键盘哪个版本最便宜",
        "limit": 3,
    }
    catalog_context = _runtime_context_for_call(state, prepared)["catalog_context"]
    assert catalog_context["memory"]["candidate_spu_ids"] == [63]
    assert catalog_context["memory"]["candidate_sku_ids"] == [745, 746, 747]
    assert catalog_context["dependency_artifacts"] == []

    ambiguous_state = cast(
        AgentState,
        {
            "user_id": 7,
            "message": "哪个版本最便宜",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {
                "catalog": {
                    "candidate_sku_ids": [745, 801],
                    "candidate_spu_ids": [63, 72],
                }
            },
            "tool_waves": [],
            "tool_results": [],
            "subquery_ledger": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )
    initialize_task_runtime(ambiguous_state)

    assert ambiguous_state["task_status"]["task_1"]["status"] == "blocked"
    assert ambiguous_state["task_status"]["task_1"]["user_can_supply"] is True
    assert _bound_targets(ambiguous_state, plan, "task_1") == []


def test_catalog_diagnostic_becomes_task_level_clarification() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "比较 Keychron K01 键盘系列",
            "subqueries": [
                {
                    "id": "goal_1",
                    "query": "比较 Keychron K01 键盘系列",
                    "disposition": "tool_planning",
                    "reason_code": "compare_series",
                    "tasks": [
                        {
                            "id": "task_1",
                            "goal_id": "goal_1",
                            "canonical_query": "比较 Keychron K01 键盘系列",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        }
                    ],
                }
            ],
        }
    )
    state = _runtime_state(plan, "比较 Keychron K01 键盘系列")
    decision = decision_from_route_capabilities(plan, state)
    assert decision is not None
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    call, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])
    result = {
        "tool_call_id": call.id,
        "name": "catalog_compare",
        "execution": {
            "tool_name": "catalog_compare",
            "ok": True,
            "output": {
                "result_type": "comparison",
                "comparison_level": "spu",
                "products": [],
                "series": [{"spu_id": 63}],
                "diagnostics": [
                    {
                        "code": "insufficient_spu_ids",
                        "severity": "error",
                        "message": "SPU comparison requires at least two explicit SPU IDs.",
                        "recommended_action": "resolve_at_least_two_spus",
                    }
                ],
            },
            "error": None,
        },
    }

    _apply_wave(state, [call.model_dump(mode="json")], [result])
    answer_context = build_answer_context(state)

    assert state["task_status"]["task_1"] == {
        "task_id": "task_1",
        "goal_id": "goal_1",
        "answer_role": "user_facing",
        "status": "blocked",
        "reason": "tool_requested_clarification:insufficient_spu_ids",
        "missing_information": ["至少两个明确的商品系列"],
        "user_can_supply": True,
    }
    assert answer_context["tasks"][0]["semantic_outcome"] == "needs_clarification"
    assert answer_context["recommended_control_action"] == "ask_clarification"
    assert "至少两个明确的商品系列" in answer_context["tasks"][0]["explanation"]


def test_scheduler_is_not_dependent_on_task_order_or_rank_two() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前显示器和销量第三的显示器，再推荐一个键盘",
            "subqueries": [
                {
                    "id": "goal_7",
                    "query": "对比当前显示器和销量第三的显示器",
                    "disposition": "tool_planning",
                    "reason_code": "compare_third_monitor",
                    "tasks": [
                        {
                            "id": "task_8",
                            "goal_id": "goal_7",
                            "canonical_query": "比较当前显示器与销量第三的显示器",
                            "depends_on": ["task_7"],
                            "input_requirements": [
                                {"name": "current", "source": "context_product"},
                                {
                                    "name": "ranked",
                                    "source": "task_output",
                                    "task_id": "task_7",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        },
                        {
                            "id": "task_7",
                            "goal_id": "goal_7",
                            "canonical_query": "查询显示器 SPU 销量排行第三的商品",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                            "result_selector": {
                                "type": "sales_rank",
                                "rank": 3,
                                "scope": "spu",
                            },
                        },
                    ],
                },
                {
                    "id": "goal_8",
                    "query": "推荐一个键盘",
                    "disposition": "tool_planning",
                    "reason_code": "recommend_keyboard",
                    "tasks": [
                        {
                            "id": "task_9",
                            "goal_id": "goal_8",
                            "canonical_query": "推荐一个键盘",
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
    state = _runtime_state(
        plan,
        "对比这个和销量第三的显示器，再推荐一个键盘",
        referenced_sku_id=990,
    )
    first = decision_from_route_capabilities(plan, state)
    assert first is not None
    assert [call.subquery for call in first.tool_calls] == ["task_7", "task_9"]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    calls = [
        runtime._prepare_tool_call(state, call)[0].model_dump(mode="json")
        for call in first.tool_calls
    ]
    ranked = [
        _product(101, 10, sales_count=9000, sku_sales_count=900),
        _product(201, 20, sales_count=8000, sku_sales_count=800),
        _product(301, 30, sales_count=7000, sku_sales_count=700),
    ]
    _apply_wave(
        state,
        calls,
        [
            _successful_result(calls[0]["id"], "catalog_search", ranked),
            _successful_result(
                calls[1]["id"],
                "catalog_search",
                [_product(401, 40, sales_count=6000, sku_sales_count=600)],
            ),
        ],
    )

    assert state["task_artifacts"]["task_7"]["value"]["selected_sku_ids"] == [301]
    assert state["task_status"]["task_8"]["status"] == "ready"
    second = decision_from_route_capabilities(plan, state)
    assert second is not None
    validated = runtime._validate_decision_budget(state, second, call_count=1)
    compare, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert compare.subquery == "task_8"
    assert compare.arguments == {
        "query": "比较当前显示器与销量第三的显示器",
        "limit": 5,
    }
    assert [item.get("sku_id") for item in _bound_targets(state, plan, "task_8")] == [
        990,
        301,
    ]


def test_named_product_compare_can_call_catalog_tool_without_discovery_tasks() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "比较 G502 和 Viper 两个鼠标型号",
            "subqueries": [
                {
                    "id": "goal_9",
                    "query": "比较 G502 和 Viper 两个鼠标型号",
                    "disposition": "tool_planning",
                    "reason_code": "compare_named_models",
                    "tasks": [
                        {
                            "id": "task_10",
                            "goal_id": "goal_9",
                            "canonical_query": "比较 G502 和 Viper 两个鼠标型号",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        }
                    ],
                }
            ],
        }
    )

    state = cast(
        AgentState,
        {
            "user_id": 7,
            "message": "比较 G502 和 Viper 两个鼠标型号",
            "route_plan": plan.model_dump(mode="json"),
            "working_memory_snapshot": {
                "catalog": {
                    "candidate_sku_ids": [901, 902],
                    "candidate_spu_ids": [90, 91],
                }
            },
        },
    )
    decision = decision_from_route_capabilities(plan, state)

    assert decision is not None
    assert [(call.name, call.subquery) for call in decision.tool_calls] == [
        ("catalog_compare", "task_10")
    ]
    assert decision.tool_calls[0].arguments == {"limit": 5}
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    prepared, _ = runtime._prepare_tool_call(state, decision.tool_calls[0])
    assert prepared.arguments == {
        "query": "比较 G502 和 Viper 两个鼠标型号",
        "limit": 5,
    }
    catalog_context = _runtime_context_for_call(state, prepared)["catalog_context"]
    assert catalog_context["memory"]["candidate_sku_ids"] == [901, 902]
    assert catalog_context["memory"]["candidate_spu_ids"] == [90, 91]
    assert catalog_context["dependency_artifacts"] == []


def test_sku_rank_and_policy_search_share_root_wave_without_cross_talk() -> None:
    plan = RequestRoutePlan.model_validate(
        {
            "rewritten_query": "对比当前键盘和 SKU 销量第二的版本，并查询退货政策",
            "subqueries": [
                {
                    "id": "goal_3",
                    "query": "对比当前键盘和 SKU 销量第二的版本",
                    "disposition": "tool_planning",
                    "reason_code": "compare_second_sku",
                    "tasks": [
                        {
                            "id": "task_4",
                            "goal_id": "goal_3",
                            "canonical_query": "查询键盘 SKU 销量排行第二的版本",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "ranked_product",
                            "answer_role": "internal",
                            "capability": "catalog_search",
                            "result_selector": {
                                "type": "sales_rank",
                                "rank": 2,
                                "scope": "sku",
                            },
                        },
                        {
                            "id": "task_5",
                            "goal_id": "goal_3",
                            "canonical_query": "比较当前键盘与 SKU 销量第二的版本",
                            "depends_on": ["task_4"],
                            "input_requirements": [
                                {"name": "current", "source": "context_product"},
                                {
                                    "name": "ranked",
                                    "source": "task_output",
                                    "task_id": "task_4",
                                },
                            ],
                            "produces": "comparison",
                            "answer_role": "user_facing",
                            "capability": "catalog_compare",
                        },
                    ],
                },
                {
                    "id": "goal_4",
                    "query": "查询退货政策",
                    "disposition": "tool_planning",
                    "reason_code": "return_policy",
                    "tasks": [
                        {
                            "id": "task_6",
                            "goal_id": "goal_4",
                            "canonical_query": "查询退货政策",
                            "depends_on": [],
                            "input_requirements": [],
                            "produces": "documents",
                            "answer_role": "user_facing",
                            "capability": "policy_search",
                        }
                    ],
                },
            ],
        }
    )
    state = _runtime_state(
        plan,
        "对比这个和 SKU 销量第二的版本，并查询退货政策",
        referenced_sku_id=600,
    )
    first = decision_from_route_capabilities(plan, state)
    assert first is not None
    assert [(call.subquery, call.name) for call in first.tool_calls] == [
        ("task_4", "catalog_search"),
        ("task_6", "policy_search"),
    ]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    calls = [
        runtime._prepare_tool_call(state, call)[0].model_dump(mode="json")
        for call in first.tool_calls
    ]
    sku_ranked = [
        _product(501, 50, sales_count=9000, sku_sales_count=900),
        _product(502, 50, sales_count=9000, sku_sales_count=800),
        _product(503, 50, sales_count=9000, sku_sales_count=700),
    ]
    policy_result = {
        "tool_call_id": calls[1]["id"],
        "name": "policy_search",
        "execution": {
            "tool_name": "policy_search",
            "ok": True,
            "output": {
                "result_type": "documents",
                "documents": [
                    {
                        "source_type": "knowledge_document",
                        "source_id": 88,
                        "title": "退货政策",
                        "document_type": "policy",
                        "snippet": "符合条件的商品可在规定期限内申请退货。",
                        "score": 0.95,
                        "metadata": {},
                    }
                ],
                "search_strategy": "hybrid",
            },
            "error": None,
        },
    }
    _apply_wave(
        state,
        calls,
        [
            _successful_result(calls[0]["id"], "catalog_search", sku_ranked),
            policy_result,
        ],
    )

    assert state["task_artifacts"]["task_4"]["value"]["selected_sku_ids"] == [502]
    assert state["task_artifacts"]["task_6"]["evidence"][0] == {
        "source_tool_call_id": calls[1]["id"],
        "source_type": "knowledge_document",
        "source_id": 88,
        "title": "退货政策",
        "document_type": "policy",
    }
    assert state["task_status"]["task_5"]["status"] == "ready"
    second = decision_from_route_capabilities(plan, state)
    assert second is not None
    validated = runtime._validate_decision_budget(state, second, call_count=1)
    compare, _ = runtime._prepare_tool_call(state, validated.tool_calls[0])
    assert compare.arguments == {
        "query": "比较当前键盘与 SKU 销量第二的版本",
        "limit": 5,
    }
    assert [item.get("sku_id") for item in _bound_targets(state, plan, "task_5")] == [
        600,
        502,
    ]


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
