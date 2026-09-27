from typing import Any, cast

import pytest

from app.agent.answer_context import build_answer_context
from app.agent.decisions import OrchestratorDecision
from app.agent.graph import AgentRuntime
from app.agent.outcomes import validate_terminal_decision
from app.agent.responses import LATE_HANDOFF_CONFIRMATION, MIXED_HANDOFF_CONFIRMATION
from app.agent.state import AgentState
from app.core.config import Settings


def _goal(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {
        "rewritten_query": "处理用户请求",
        "subqueries": [
            {
                "id": "goal_1",
                "query": "处理用户请求",
                "disposition": "tool_planning",
                "reason_code": "test",
                "tasks": list(tasks),
            }
        ],
    }


def _task(
    task_id: str,
    query: str,
    *,
    produces: str,
    capability: str,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "goal_id": "goal_1",
        "canonical_query": query,
        "depends_on": [],
        "input_requirements": [],
        "produces": produces,
        "answer_role": "user_facing",
        "capability": capability,
    }


def _artifact(
    task_id: str,
    call_id: str,
    tool_name: str,
    artifact_type: str,
    *,
    usable: bool,
    value: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "goal_id": "goal_1",
        "artifact_type": artifact_type,
        "usable": usable,
        "value": value,
        "evidence": [{"source_tool_call_id": call_id}] if usable else [],
        "source_tool_call_id": call_id,
        "source_tool_name": tool_name,
        "extractor": "deterministic",
        "reason": reason,
    }


def _ledger(
    task_id: str,
    call_id: str,
    tool_name: str,
    outcome: str,
    *,
    usable: bool,
) -> dict[str, Any]:
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "subquery": task_id,
        "status": "ready_to_answer" if usable else "unavailable",
        "outcome": outcome,
        "has_usable_information": usable,
        "reason": f"test:{outcome}",
        "wave": 1,
        "arguments": {},
    }


def test_facets_answer_context_requires_brand_values_not_count_sum() -> None:
    task = _task(
        "task_1",
        "查询键盘有哪些品牌",
        produces="facets",
        capability="catalog_facets",
    )
    items = [
        {"value": "Akko", "count": 96},
        {"value": "Keychron", "count": 96},
        {"value": "Razer", "count": 96},
        {"value": "Wooting", "count": 96},
    ]
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "facets-1",
                "catalog_facets",
                "facets",
                usable=True,
                value={"facet": "brand", "items": items},
                reason="facets_found",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "facets-1", "catalog_facets", "usable", usable=True)
        ],
    }

    context = build_answer_context(state)
    answer_task = context["tasks"][0]

    assert context["rewritten_query"] == "处理用户请求"
    assert context["aggregation_contract"]["coverage_target"] == "处理用户请求"
    assert "业务事实来源" in context["aggregation_contract"]["forbidden"][0]
    assert context["completion"] == "full"
    assert answer_task["semantic_outcome"] == "answered_with_facts"
    assert answer_task["response_contract"]["must_include_values"] == [
        "Akko",
        "Keychron",
        "Razer",
        "Wooting",
    ]
    assert "只汇总 count 而省略 value" in answer_task["response_contract"]["forbidden"]


def test_spu_comparison_answer_contract_uses_series_as_primary_evidence() -> None:
    task = _task(
        "task_1",
        "对比两个鼠标系列",
        produces="comparison",
        capability="catalog_compare",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "compare-1",
                "catalog_compare",
                "comparison",
                usable=True,
                value={
                    "comparison_level": "spu",
                    "products": [],
                    "series": [{"spu_id": 1}, {"spu_id": 2}],
                    "series_differences": [],
                },
                reason="comparison_has_two_or_more_series",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "compare-1", "catalog_compare", "usable", usable=True)
        ],
    }

    context = build_answer_context(state)
    contract = context["tasks"][0]["response_contract"]

    assert "区分全系列共同规格与仅部分 SKU 提供的可选规格" in contract["required"]
    assert "用单个代表 SKU 的规格概括整个系列" in contract["forbidden"]
    assert "把不同可选规格自由组合成不存在的 SKU" in contract["forbidden"]


def test_spu_ranking_answer_contract_rejects_representative_sku_semantics() -> None:
    task = _task(
        "task_1",
        "查询库存最多的键盘",
        produces="ranked_product",
        capability="catalog_search",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "search-1",
                "catalog_search",
                "ranked_product",
                usable=True,
                value={
                    "products": [
                        {
                            "spu_id": 10,
                            "sku_id": 101,
                            "title": "辅助 SKU",
                            "spu_title": "测试键盘系列",
                            "ranking_scope": "spu",
                            "ranking_metric": "stock",
                            "ranking_value": "42",
                            "stock": 7,
                            "series_total_stock": 42,
                        }
                    ]
                },
                reason="ranked_product_selected",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "search-1", "catalog_search", "usable", usable=True)
        ],
    }

    contract = build_answer_context(state)["tasks"][0]["response_contract"]

    assert "明确这是商品系列/SPU 排名，并使用 ranking_value 说明排名依据" in contract[
        "required"
    ]
    assert "把辅助 SKU 的 stock 当成系列总库存" in contract["forbidden"]
    assert "把辅助 SKU 的 title 当成系列名称（有 spu_title 时使用 spu_title）" in contract[
        "forbidden"
    ]


def test_spu_recommendation_answer_contract_uses_series_options_not_auxiliary_sku() -> None:
    task = _task(
        "task_1",
        "推荐一个键盘",
        produces="products",
        capability="catalog_search",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "search-1",
                "catalog_search",
                "products",
                usable=True,
                value={
                    "products": [
                        {
                            "spu_id": 10,
                            "sku_id": 101,
                            "title": "黑色辅助 SKU",
                            "spu_title": "测试键盘系列",
                            "entity_scope": "spu",
                            "series_min_price": "300",
                            "series_max_price": "320",
                            "series_total_stock": 42,
                            "series_common_specs": {},
                            "series_option_specs": {
                                "connection_type": ["有线", "蓝牙"]
                            },
                            "series_variants": [{"sku_id": 101}, {"sku_id": 102}],
                        }
                    ]
                },
                reason="products_found",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "search-1", "catalog_search", "usable", usable=True)
        ],
    }

    contract = build_answer_context(state)["tasks"][0]["response_contract"]

    assert "把返回对象作为商品系列/SPU，使用 spu_title 作为系列名称" in contract[
        "required"
    ]
    assert "规格只使用 series_common_specs、series_option_specs 和 series_variants" in contract[
        "required"
    ]
    assert not any("SPU 排名" in item for item in contract["required"])
    assert "把辅助 SKU 的 specs 当成全系列共同规格" in contract["forbidden"]


def test_recommendation_answer_contract_distinguishes_total_from_candidate_window() -> None:
    task = _task(
        "task_1",
        "你最推荐哪个版本",
        produces="products",
        capability="catalog_search",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "search-1",
                "catalog_search",
                "products",
                usable=True,
                value={
                    "result_purpose": "recommendation",
                    "selection_scope": "sku",
                    "total_match_count": 12,
                    "returned_count": 3,
                    "is_exhaustive": False,
                    "products": [
                        {"spu_id": 10, "sku_id": 101, "title": "首选版本"},
                        {"spu_id": 10, "sku_id": 102, "title": "备选版本 A"},
                        {"spu_id": 10, "sku_id": 103, "title": "备选版本 B"},
                    ],
                },
                reason="products_found",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "search-1", "catalog_search", "usable", usable=True)
        ],
    }

    contract = build_answer_context(state)["tasks"][0]["response_contract"]

    assert contract["result_window"] == {
        "result_purpose": "recommendation",
        "selection_scope": "sku",
        "total_match_count": 12,
        "returned_count": 3,
        "is_exhaustive": False,
    }
    assert any("共匹配 12 个具体版本/SKU" in item for item in contract["required"])
    assert any("第一项作为首选" in item for item in contract["required"])
    assert any("只有本次返回的 3 个结果" in item for item in contract["forbidden"])


def test_near_match_answer_contract_requires_disclosing_each_relaxed_filter() -> None:
    task = _task(
        "task_1",
        "推荐接近 4K、USB-C、60fps 的摄像头",
        produces="products",
        capability="catalog_search",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "search-1",
                "catalog_search",
                "products",
                usable=True,
                value={
                    "result_purpose": "recommendation",
                    "selection_scope": "sku",
                    "total_match_count": 2,
                    "returned_count": 2,
                    "is_exhaustive": True,
                    "products": [
                        {
                            "spu_id": 10,
                            "sku_id": 101,
                            "title": "USB-C 1440p 摄像头",
                        },
                        {
                            "spu_id": 20,
                            "sku_id": 201,
                            "title": "USB-A 4K 摄像头",
                        },
                    ],
                    "query_plan": {
                        "relaxation": {
                            "applied": True,
                            "mode": "single_filter",
                            "exact_match_count": 0,
                            "requested_filters": {
                                "resolution": "4K",
                                "connection_type": "USB-C",
                                "frame_rate": "60fps",
                            },
                            "candidate_differences": {
                                "101": {
                                    "resolution": {
                                        "requested": "4K",
                                        "actual": "1440p",
                                    }
                                },
                                "201": {
                                    "connection_type": {
                                        "requested": "USB-C",
                                        "actual": "USB-A",
                                    }
                                },
                            },
                        }
                    },
                },
                reason="products_found",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "search-1", "catalog_search", "usable", usable=True)
        ],
    }

    contract = build_answer_context(state)["tasks"][0]["response_contract"]

    assert contract["near_match"]["exact_match_count"] == 0
    assert contract["near_match"]["candidate_differences"]["101"]["resolution"] == {
        "requested": "4K",
        "actual": "1440p",
    }
    assert any("没有完全满足" in item for item in contract["required"])
    assert any("逐款" in item for item in contract["required"])
    assert any("不同放宽维度" in item for item in contract["required"])
    assert any("完全满足全部" in item for item in contract["forbidden"])
    assert any("全局按销量排序" in item for item in contract["forbidden"])
    assert not any("第一项作为首选" in item for item in contract["required"])


def test_no_match_is_a_fully_answered_negative_result() -> None:
    task = _task(
        "task_1",
        "查询十元以内的 4K 显示器",
        produces="products",
        capability="catalog_search",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {
            "task_1": {"status": "unavailable", "reason": "tool_outcome:empty"}
        },
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "search-1",
                "catalog_search",
                "products",
                usable=False,
                value=None,
                reason="no_matching_products",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "search-1", "catalog_search", "empty", usable=False)
        ],
    }

    context = build_answer_context(state)
    decision = OrchestratorDecision(
        type="grounded_response",
        response="当前没有找到十元以内的 4K 显示器。",
        control_action="finish_answer",
        used_tool_call_ids=["search-1"],
    )

    assert context["completion"] == "full"
    assert context["tasks"][0]["semantic_outcome"] == "answered_no_match"
    assert context["recommended_control_action"] == "finish_answer"
    assert validate_terminal_decision(
        decision,
        state["subquery_ledger"],
        planned_subquery_ids=["task_1"],
        resolved_task_ids=context["resolved_task_ids"],
        answerable_tool_call_ids=context["answerable_source_tool_call_ids"],
    ).valid


@pytest.mark.asyncio
async def test_usage_mapping_no_match_does_not_trigger_handoff() -> None:
    task = _task(
        "task_1",
        "推荐办公鼠标",
        produces="products",
        capability="catalog_search",
    )
    ledger = _ledger(
        "task_1",
        "search-1",
        "catalog_search",
        "empty",
        usable=False,
    )
    ledger["reason"] = "usage_mapping_unavailable"
    state = cast(
        AgentState,
        {
            "message": "推荐办公鼠标",
            "route_plan": _goal(task),
            "task_status": {
                "task_1": {"status": "unavailable", "reason": "tool_outcome:empty"}
            },
            "task_artifacts": {
                "task_1": _artifact(
                    "task_1",
                    "search-1",
                    "catalog_search",
                    "products",
                    usable=False,
                    value=None,
                    reason="usage_mapping_unavailable",
                )
            },
            "subquery_ledger": [ledger],
            "tool_results": [
                {
                    "tool_call_id": "search-1",
                    "name": "catalog_search",
                    "execution": {
                        "ok": True,
                        "output": {
                            "result_type": "empty",
                            "products": [],
                            "ranking_strategy": "unsupported_query",
                            "query_plan": {
                                "supported": False,
                                "error_type": "usage_mapping_unavailable",
                            },
                            "diagnostics": [{"code": "usage_mapping_unavailable"}],
                        },
                    },
                }
            ],
            "decision": {
                "type": "grounded_response",
                "response": "当前目录没有找到能依据现有规格确认适合办公场景的鼠标。",
                "control_action": "finish_answer",
                "used_tool_call_ids": ["search-1"],
                "offer_handoff_confirmation": True,
            },
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "可自动回答",
                "display_message": "可自动回答",
            },
            "products": [],
            "evidence": [],
            "order": None,
        },
    )

    context = build_answer_context(state)
    answer_task = context["tasks"][0]

    assert context["completion"] == "full"
    assert context["recommended_control_action"] == "finish_answer"
    assert answer_task["semantic_outcome"] == "answered_no_match"
    assert "限定于场景证明条件" in answer_task["explanation"]
    assert any(
        "依据现有规格确认匹配该场景" in item
        for item in answer_task["response_contract"]["required"]
    )
    assert any(
        "没有该品类商品" in item
        for item in answer_task["response_contract"]["forbidden"]
    )

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    guarded = await runtime._terminal_guard(state)
    finalized = await runtime._finalize_response(guarded)

    assert guarded["terminal_guard_status"] == "accepted"
    assert guarded["decision"]["offer_handoff_confirmation"] is False
    assert finalized["boundary"]["classification"] == "in_scope_auto"
    assert finalized["answer"] == "当前目录没有找到能依据现有规格确认适合办公场景的鼠标。"
    assert not any(
        action.get("payload", {}).get("handoff")
        for action in finalized["suggested_actions"]
    )


@pytest.mark.asyncio
async def test_answer_context_aggregates_partial_results_and_offers_handoff() -> None:
    catalog_task = _task(
        "task_1",
        "推荐无线鼠标",
        produces="products",
        capability="catalog_search",
    )
    trend_task = _task(
        "task_2",
        "分析过去一年的销量增长率",
        produces="products",
        capability="catalog_search",
    )
    state = cast(
        AgentState,
        {
            "message": "推荐无线鼠标，并分析过去一年的销量增长率",
            "route_plan": _goal(catalog_task, trend_task),
            "task_status": {
                "task_1": {"status": "succeeded"},
                "task_2": {"status": "unavailable", "reason": "tool_outcome:unsupported"},
            },
            "task_artifacts": {
                "task_1": _artifact(
                    "task_1",
                    "search-1",
                    "catalog_search",
                    "products",
                    usable=True,
                    value={"products": [{"title": "Mouse"}], "query_plan": {}},
                    reason="products_found",
                ),
                "task_2": _artifact(
                    "task_2",
                    "search-2",
                    "catalog_search",
                    "products",
                    usable=False,
                    value=None,
                    reason="query_not_supported_by_tool",
                ),
            },
            "subquery_ledger": [
                _ledger("task_1", "search-1", "catalog_search", "usable", usable=True),
                _ledger(
                    "task_2",
                    "search-2",
                    "catalog_search",
                    "unsupported",
                    usable=False,
                ),
            ],
            "decision": {
                "type": "partial_response",
                "response": "已找到可推荐的无线鼠标。",
                "control_action": "finish_partial",
                "used_tool_call_ids": ["search-1"],
                "unavailable_parts": ["分析过去一年的销量增长率"],
                "offer_handoff_confirmation": False,
            },
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "可自动回答",
                "display_message": "可自动回答",
            },
            "products": [],
            "evidence": [],
            "order": None,
        },
    )

    context = build_answer_context(state)

    assert context["completion"] == "partial"
    assert context["recommended_control_action"] == "finish_partial"
    assert context["answerable_source_tool_call_ids"] == ["search-1"]
    assert [task["semantic_outcome"] for task in context["tasks"]] == [
        "answered_with_facts",
        "unsupported_capability",
    ]
    assert context["unavailable_parts"] == ["分析过去一年的销量增长率"]

    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    guarded = await runtime._terminal_guard(state)
    finalized = await runtime._finalize_response(guarded)

    assert guarded["terminal_guard_status"] == "accepted"
    assert guarded["decision"]["offer_handoff_confirmation"] is True
    assert finalized["boundary"]["classification"] == "human_handoff_required"
    assert finalized["answer"].startswith("已找到可推荐的无线鼠标。")
    assert finalized["answer"].endswith(MIXED_HANDOFF_CONFIRMATION)
    assert finalized["suggested_actions"][0]["payload"]["handoff"] is True


def test_order_analysis_contract_requires_question_focused_minimal_disclosure() -> None:
    task = _task(
        "task_1",
        "我买过雷蛇鼠标吗",
        produces="order",
        capability="order_lookup",
    )
    state = {
        "route_plan": _goal(task),
        "task_status": {"task_1": {"status": "succeeded"}},
        "task_artifacts": {
            "task_1": _artifact(
                "task_1",
                "order-1",
                "order_lookup",
                "order",
                usable=True,
                value={
                    "result_type": "order_analysis",
                    "query_mode": "analysis",
                    "analysis_orders": [
                        {"id": 1, "items": [{"sku_name": "Razer Viper 鼠标"}]},
                        {"id": 2, "items": [{"sku_name": "其他无关键盘"}]},
                    ],
                    "total_match_count": 2,
                    "returned_count": 2,
                    "is_exhaustive": True,
                },
                reason="order_analysis_available",
            )
        },
        "subquery_ledger": [
            _ledger("task_1", "order-1", "order_lookup", "usable", usable=True)
        ],
    }

    context = build_answer_context(state)
    contract = context["tasks"][0]["response_contract"]

    assert context["completion"] == "full"
    assert any("只回答用户实际询问" in item for item in contract["required"])
    assert any("全部订单逐项复述" in item for item in contract["forbidden"])
    assert any("与用户问题无关" in item for item in contract["forbidden"])


@pytest.mark.asyncio
async def test_order_observation_missing_requested_address_triggers_late_handoff() -> None:
    task = _task(
        "task_1",
        "给我一份所有订单的收货地址表",
        produces="order",
        capability="order_lookup",
    )
    state = cast(
        AgentState,
        {
            "message": "给我一份所有订单的收货地址表",
            "route_plan": _goal(task),
            "task_status": {"task_1": {"status": "succeeded"}},
            "task_artifacts": {
                "task_1": _artifact(
                    "task_1",
                    "order-1",
                    "order_lookup",
                    "order",
                    usable=True,
                    value={
                        "result_type": "order_candidates",
                        "query_mode": "list",
                        "total_match_count": 2,
                        "returned_count": 2,
                        "is_exhaustive": True,
                        "candidates": [
                            {
                                "id": 101,
                                "status": "shipped",
                                "pay_amount": "599.00",
                                "created_at": "2026-07-01T12:00:00",
                                "item_count": 1,
                                "first_item_name": "测试键盘",
                                "logistic_no": "SF001",
                            }
                        ],
                    },
                    reason="order_candidates_found",
                )
            },
            "subquery_ledger": [
                _ledger("task_1", "order-1", "order_lookup", "usable", usable=True)
            ],
            "decision": {
                "type": "unavailable_response",
                "response": "订单查询结果没有包含收货地址，无法生成地址表。",
                "control_action": "finish_unavailable",
                "unavailable_parts": ["所有订单的收货地址"],
                "observation_coverage_failure": True,
            },
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "可自动回答",
                "display_message": "可自动回答",
            },
            "intent": "order_lookup",
            "products": [],
            "evidence": [],
            "order": None,
        },
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))

    context = build_answer_context(state)
    contract = context["tasks"][0]["response_contract"]
    assert context["completion"] == "full"
    assert contract["coverage_gate"]["not_provided_fields"] == [
        "收件人姓名",
        "收件人手机号",
        "收货地址",
        "支付方式",
        "预计送达时间或还需多久到货",
        "批量地址表或导出文件",
    ]
    assert "finish_unavailable" in contract["coverage_gate"]["on_missing_core_field"]
    assert any("订单号、状态、订单总金额、实付金额" in item for item in contract["required"])
    assert any("收货地址" in item for item in contract["forbidden"])
    assert runtime._orchestrator_model_for_state(state) is runtime.answer_synthesizer

    guarded = await runtime._terminal_guard(state)
    finalized = await runtime._finalize_response(guarded)

    assert guarded["terminal_guard_status"] == "accepted"
    assert guarded["decision"]["observation_coverage_failure"] is True
    assert guarded["decision"]["offer_handoff_confirmation"] is True
    assert finalized["boundary"]["classification"] == "human_handoff_required"
    assert finalized["answer"] == LATE_HANDOFF_CONFIRMATION


@pytest.mark.asyncio
async def test_unsupported_tool_outcome_switches_to_generic_handoff_confirmation() -> None:
    task = _task(
        "task_1",
        "看看这个问题能不能处理",
        produces="documents",
        capability="knowledge_search",
    )
    state = cast(
        AgentState,
        {
            "message": "看看这个问题能不能处理",
            "route_plan": _goal(task),
            "task_status": {
                "task_1": {
                    "status": "unavailable",
                    "reason": "tool_outcome:unsupported",
                }
            },
            "task_artifacts": {
                "task_1": _artifact(
                    "task_1",
                    "knowledge-1",
                    "knowledge_search",
                    "documents",
                    usable=False,
                    value=None,
                    reason="query_not_supported_by_tool",
                )
            },
            "subquery_ledger": [
                _ledger(
                    "task_1",
                    "knowledge-1",
                    "knowledge_search",
                    "unsupported",
                    usable=False,
                )
            ],
            "tool_results": [
                {
                    "tool_call_id": "knowledge-1",
                    "name": "knowledge_search",
                    "execution": {
                        "ok": True,
                        "output": {"result_type": "empty", "documents": []},
                    },
                }
            ],
            "decision": {
                "type": "unavailable_response",
                "response": "现有信息不足以判断你是否希望办理操作。",
                "control_action": "finish_unavailable",
                "unavailable_parts": ["看看这个问题能不能处理"],
                "offer_handoff_confirmation": True,
            },
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "可自动回答",
                "display_message": "可自动回答",
            },
            "intent": "knowledge_search",
            "products": [],
            "evidence": [],
            "order": None,
        },
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))

    guarded = await runtime._terminal_guard(state)
    finalized = await runtime._finalize_response(guarded)

    assert finalized["terminal_guard_status"] == "accepted"
    assert finalized["boundary"]["classification"] == "human_handoff_required"
    assert finalized["answer"] == LATE_HANDOFF_CONFIRMATION
    assert "看看这个问题能不能处理" not in finalized["answer"]
    assert finalized["suggested_actions"] == [
        {
            "label": "转人工客服",
            "payload": {
                "handoff": True,
                "orderId": None,
                "requestType": "other",
                "reason": "看看这个问题能不能处理",
            },
        }
    ]
    assert finalized["decision"]["offer_handoff_confirmation"] is True

    detailed_unavailable = "当前知识库不支持查询这项信息。"
    state["decision"] = {
        "type": "unavailable_response",
        "response": detailed_unavailable,
        "control_action": "finish_unavailable",
        "unavailable_parts": ["看看这个问题能不能处理"],
        "offer_handoff_confirmation": False,
    }
    state["boundary"] = {
        "classification": "in_scope_auto",
        "reason": "可自动回答",
        "display_message": "可自动回答",
    }
    guarded = await runtime._terminal_guard(state)
    assert guarded["decision"]["offer_handoff_confirmation"] is True
    finalized = await runtime._finalize_response(guarded)

    assert finalized["terminal_guard_status"] == "accepted"
    assert finalized["answer"] == LATE_HANDOFF_CONFIRMATION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "semantic_outcome", "control_action"),
    [
        ("empty", "answered_no_match", "finish_answer"),
        ("not_found", "answered_no_match", "finish_answer"),
        ("insufficient", "insufficient_evidence", "finish_unavailable"),
        ("error", "temporarily_unavailable", "finish_unavailable"),
    ],
)
async def test_non_unsupported_tool_outcomes_do_not_trigger_handoff(
    outcome: str,
    semantic_outcome: str,
    control_action: str,
) -> None:
    task = _task(
        "task_1",
        "查询商城信息",
        produces="documents",
        capability="knowledge_search",
    )
    is_resolved = outcome in {"empty", "not_found"}
    execution = (
        {
            "ok": False,
            "error": {
                "code": "timeout",
                "message": "timed out",
                "retryable": True,
            },
        }
        if outcome == "error"
        else {
            "ok": True,
            "output": {"result_type": "empty", "documents": []},
        }
    )
    decision = {
        "type": "grounded_response" if is_resolved else "unavailable_response",
        "response": "当前没有匹配结果。" if is_resolved else "当前暂时无法回答。",
        "control_action": control_action,
        "used_tool_call_ids": ["knowledge-1"] if is_resolved else [],
        "unavailable_parts": [] if is_resolved else ["查询商城信息"],
        "offer_handoff_confirmation": True,
    }
    state = cast(
        AgentState,
        {
            "message": "查询商城信息",
            "route_plan": _goal(task),
            "task_status": {
                "task_1": {
                    "status": "unavailable",
                    "reason": f"tool_outcome:{outcome}",
                }
            },
            "task_artifacts": {
                "task_1": _artifact(
                    "task_1",
                    "knowledge-1",
                    "knowledge_search",
                    "documents",
                    usable=False,
                    value=None,
                    reason=f"test:{outcome}",
                )
            },
            "subquery_ledger": [
                _ledger(
                    "task_1",
                    "knowledge-1",
                    "knowledge_search",
                    outcome,
                    usable=False,
                )
            ],
            "tool_results": [
                {
                    "tool_call_id": "knowledge-1",
                    "name": "knowledge_search",
                    "execution": execution,
                }
            ],
            "decision": decision,
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "可自动回答",
                "display_message": "可自动回答",
            },
            "intent": "knowledge_search",
            "products": [],
            "evidence": [],
            "order": None,
        },
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))

    context = build_answer_context(state)
    guarded = await runtime._terminal_guard(state)
    finalized = await runtime._finalize_response(guarded)

    assert context["tasks"][0]["semantic_outcome"] == semantic_outcome
    assert guarded["terminal_guard_status"] == "accepted"
    assert guarded["decision"]["offer_handoff_confirmation"] is False
    assert finalized["boundary"]["classification"] == "in_scope_auto"
    assert finalized["suggested_actions"] == []
    assert LATE_HANDOFF_CONFIRMATION not in finalized["answer"]


def test_handoff_confirmation_is_rejected_for_a_full_answer() -> None:
    decision = OrchestratorDecision(
        type="grounded_response",
        response="已经完整回答。",
        control_action="finish_answer",
        used_tool_call_ids=["search-1"],
        offer_handoff_confirmation=True,
    )

    validation = validate_terminal_decision(
        decision,
        [
            _ledger(
                "task_1",
                "search-1",
                "catalog_search",
                "usable",
                usable=True,
            )
        ],
        planned_subquery_ids=["task_1"],
        resolved_task_ids=["task_1"],
        answerable_tool_call_ids=["search-1"],
        handoff_confirmation_allowed=True,
    )

    assert not validation.valid
    assert validation.reason == "handoff_confirmation_not_allowed_for_terminal_state"


def test_terminal_guard_validation_rejects_boundary_changes() -> None:
    decision = OrchestratorDecision(
        type="unavailable_response",
        response="当前无法回答。",
        control_action="finish_unavailable",
        unavailable_parts=["当前问题"],
    )

    validation = validate_terminal_decision(
        decision,
        [
            _ledger(
                "task_1",
                "search-1",
                "catalog_search",
                "unsupported",
                usable=False,
            )
        ],
        boundary_consistent=False,
    )

    assert not validation.valid
    assert validation.reason == "terminal_boundary_changed"
