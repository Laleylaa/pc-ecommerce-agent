from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage

from app.agent.artifacts import initialize_task_runtime
from app.agent.decisions import OrchestratorDecision, PlannedToolCall, decision_from_ai_message
from app.agent.graph import (
    AgentRuntime,
    _fallback_answer,
    _fallback_unavailable_answer,
    _has_successful_tool_result,
)
from app.agent.outcomes import (
    build_subquery_ledger,
    has_usable_information,
    normalize_tool_result,
    tool_call_fingerprint,
    validate_terminal_decision,
)
from app.agent.responses import LATE_HANDOFF_CONFIRMATION
from app.agent.state import AgentState
from app.core.config import Settings
from app.schemas.catalog import ProductCard


def _result(
    name: str,
    output: dict[str, Any] | None = None,
    *,
    call_id: str = "call-1",
    ok: bool = True,
    error_code: str = "timeout",
) -> dict[str, Any]:
    execution: dict[str, Any] = {"ok": ok}
    if ok:
        execution["output"] = output
    else:
        execution["error"] = {"code": error_code}
    return {
        "tool_call_id": call_id,
        "name": name,
        "execution": execution,
    }


def _control_message(name: str, **arguments: Any) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": f"control-{name}",
                "name": name,
                "args": arguments,
                "type": "tool_call",
            }
        ],
    )


def test_spu_comparison_is_usable_when_two_series_are_present() -> None:
    outcome = normalize_tool_result(
        _result(
            "catalog_compare",
            {
                "result_type": "comparison",
                "comparison_level": "spu",
                "products": [],
                "series": [{"spu_id": 10}, {"spu_id": 20}],
            },
        )
    )

    assert outcome.outcome == "usable"
    assert outcome.has_usable_information is True
    assert outcome.reason == "comparison_has_two_or_more_series"


def _tool_route_plan(query: str = "查询业务信息") -> dict[str, Any]:
    return {
        "rewritten_query": query,
        "subqueries": [
            {
                "id": "sq_1",
                "query": query,
                "disposition": "tool_planning",
                "reason_code": "test_tool_planning",
            }
        ],
    }


@pytest.mark.parametrize(
    ("result", "expected_outcome", "usable"),
    [
        (
            _result(
                "catalog_search",
                {"result_type": "products", "products": [{"sku_id": 1}]},
            ),
            "usable",
            True,
        ),
        (
            _result("catalog_search", {"result_type": "empty", "products": []}),
            "empty",
            False,
        ),
        (
            _result(
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "diagnostics": [{"code": "invalid_catalog_plan"}],
                },
            ),
            "insufficient",
            False,
        ),
        (
            _result(
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "diagnostics": [{"code": "unsupported_query"}],
                },
            ),
            "unsupported",
            False,
        ),
        (
            _result(
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "unsupported_query",
                    "query_plan": {
                        "supported": False,
                        "error_type": "usage_mapping_unavailable",
                        "usage_mapping": {"status": "unavailable"},
                    },
                    "diagnostics": [{"code": "usage_mapping_unavailable"}],
                },
            ),
            "empty",
            False,
        ),
        (
            _result(
                "catalog_search",
                {"result_type": "empty", "products": [{"sku_id": 1}]},
            ),
            "insufficient",
            False,
        ),
        (
            _result(
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "unsupported_query",
                    "query_plan": {"supported": False},
                },
            ),
            "unsupported",
            False,
        ),
        (
            _result(
                "catalog_compare",
                {"result_type": "comparison", "products": [{"sku_id": 1}]},
            ),
            "insufficient",
            False,
        ),
        (
            _result(
                "catalog_facets",
                {"result_type": "facets", "items": [{"value": "Logitech"}]},
            ),
            "usable",
            True,
        ),
        (
            _result("order_lookup", {"result_type": "not_found", "candidates": []}),
            "not_found",
            False,
        ),
        (
            _result(
                "order_lookup",
                {
                    "result_type": "unsupported",
                    "diagnostics": [{"code": "unsupported_query"}],
                    "query_plan": {
                        "supported": False,
                        "unsupported_fields": ["receiver_address"],
                    },
                },
            ),
            "unsupported",
            False,
        ),
        (
            _result(
                "order_lookup",
                {
                    "result_type": "planning_failed",
                    "diagnostics": [{"code": "invalid_order_plan"}],
                },
            ),
            "insufficient",
            False,
        ),
        (
            _result(
                "order_lookup",
                {
                    "result_type": "single_order",
                    "order": {"id": 202607020001},
                    "diagnostics": [{"code": "partial_unsupported_query"}],
                    "query_plan": {
                        "supported": True,
                        "fully_supported": False,
                        "unsupported_fields": ["payment_method"],
                    },
                },
            ),
            "usable",
            True,
        ),
        (
            _result("knowledge_search", {"result_type": "empty", "documents": []}),
            "empty",
            False,
        ),
        (_result("policy_search", ok=False), "error", False),
    ],
)
def test_normalize_tool_result_distinguishes_execution_from_usable_information(
    result: dict[str, Any],
    expected_outcome: str,
    usable: bool,
) -> None:
    outcome = normalize_tool_result(result)

    assert outcome.outcome == expected_outcome
    assert outcome.has_usable_information is usable


def test_subquery_ledger_preserves_call_identity_arguments_and_outcome() -> None:
    waves = [
        {
            "wave": 1,
            "calls": [
                {
                    "id": "catalog-1",
                    "name": "catalog_search",
                    "arguments": {"query": "销量趋势"},
                    "subquery": "查询鼠标销量趋势",
                }
            ],
            "results": [
                _result(
                    "catalog_search",
                    {
                        "result_type": "empty",
                        "products": [],
                        "ranking_strategy": "unsupported_query",
                    },
                    call_id="catalog-1",
                )
            ],
        }
    ]

    ledger = build_subquery_ledger(waves)

    assert len(ledger) == 1
    assert ledger[0].tool_call_id == "catalog-1"
    assert ledger[0].subquery == "查询鼠标销量趋势"
    assert ledger[0].subquery_id.startswith("sq_")
    assert ledger[0].canonical_query == "销量趋势"
    assert ledger[0].query_fingerprint
    assert ledger[0].initial_tool_call_id == "catalog-1"
    assert ledger[0].status == "unavailable"
    assert ledger[0].arguments == {"query": "销量趋势"}
    assert ledger[0].outcome == "unsupported"
    assert ledger[0].fingerprint
    assert ledger[0].reused_from_tool_call_id is None
    assert has_usable_information([item.model_dump() for item in ledger]) is False


def test_usable_outcome_is_ready_to_answer_not_implicitly_request_complete() -> None:
    waves = [
        {
            "wave": 1,
            "calls": [
                {
                    "id": "knowledge-1",
                    "name": "knowledge_search",
                    "arguments": {"query": "DPI 怎么选"},
                    "subquery": "解释 DPI 怎么选",
                }
            ],
            "results": [
                _result(
                    "knowledge_search",
                    {
                        "result_type": "documents",
                        "documents": [{"title": "鼠标 DPI 指南"}],
                    },
                    call_id="knowledge-1",
                )
            ],
        }
    ]

    ledger = build_subquery_ledger(waves)

    assert ledger[0].outcome == "usable"
    assert ledger[0].status == "ready_to_answer"
    assert ledger[0].subquery == "解释 DPI 怎么选"


def test_new_attempt_supersedes_old_status_for_the_same_subquery() -> None:
    waves = [
        {
            "wave": 1,
            "calls": [
                {
                    "id": "catalog-1",
                    "name": "catalog_search",
                    "arguments": {"query": "无线鼠标", "brands": ["Razer"]},
                    "subquery": "推荐无线鼠标",
                }
            ],
            "results": [
                _result(
                    "catalog_search",
                    {"result_type": "empty", "products": []},
                    call_id="catalog-1",
                )
            ],
        },
        {
            "wave": 2,
            "calls": [
                {
                    "id": "catalog-2",
                    "name": "catalog_search",
                    "arguments": {"query": "无线鼠标"},
                    "subquery": "推荐无线鼠标",
                }
            ],
            "results": [
                _result(
                    "catalog_search",
                    {
                        "result_type": "products",
                        "products": [{"sku_id": 1}],
                    },
                    call_id="catalog-2",
                )
            ],
        },
    ]

    ledger = build_subquery_ledger(waves)

    assert [entry.status for entry in ledger] == ["superseded", "ready_to_answer"]
    assert [entry.canonical_query for entry in ledger] == ["无线鼠标", "无线鼠标"]
    assert ledger[0].query_fingerprint == ledger[1].query_fingerprint
    assert ledger[0].subquery_id == ledger[1].subquery_id
    assert ledger[1].initial_tool_call_id == "catalog-1"


def test_tool_call_fingerprint_normalizes_equivalent_arguments() -> None:
    first = tool_call_fingerprint(
        "catalog_search",
        {
            "query": "  Wireless   Mouse ",
            "brands": ["Razer", "Logitech"],
            "filters": {"connection_type": "Wireless"},
            "category": None,
        },
    )
    equivalent = tool_call_fingerprint(
        "CATALOG_SEARCH",
        {
            "filters": {"connection_type": "wireless"},
            "brands": ["logitech", "razer"],
            "query": "wireless mouse",
        },
    )
    different = tool_call_fingerprint(
        "catalog_search",
        {
            "query": "wireless mouse",
            "brands": ["Logitech"],
            "filters": {"connection_type": "Wireless"},
        },
    )

    assert first == equivalent
    assert first != different


@pytest.mark.asyncio
async def test_graph_normalization_nodes_publish_the_ledger_to_state() -> None:
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    tool_result = _result(
        "knowledge_search",
        {"result_type": "empty", "documents": []},
        call_id="knowledge-1",
    )
    state = cast(
        AgentState,
        {
            "tool_results": [tool_result],
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "knowledge-1",
                            "name": "knowledge_search",
                            "arguments": {"query": "冷门问题"},
                        }
                    ],
                    "results": [tool_result],
                }
            ],
        },
    )

    normalized = await runtime._normalize_tool_results(state)
    updated = await runtime._update_subquery_ledger(normalized)

    assert updated["normalized_tool_results"][0]["outcome"] == "empty"
    assert updated["subquery_ledger"][0]["tool_call_id"] == "knowledge-1"
    assert updated["subquery_ledger"][0]["arguments"] == {"query": "冷门问题"}


def test_ok_empty_result_is_not_reported_as_successful_evidence() -> None:
    state = cast(
        AgentState,
        {"tool_results": [_result("catalog_search", {"result_type": "empty", "products": []})]},
    )

    assert _has_successful_tool_result(state) is False


def test_catalog_fallback_distinguishes_sku_and_spu_sales_counts() -> None:
    state = cast(
        AgentState,
        {
            "message": "这款鼠标销量多少？",
            "products": [
                ProductCard(
                    spu_id=10,
                    sku_id=101,
                    title="Razer Test Mouse 黑色",
                    brand="Razer",
                    category="mouse",
                    price="399.00",
                    stock=8,
                    sku_sales_count=5,
                    sales_count=12,
                )
            ],
        },
    )

    answer = _fallback_answer(state)

    assert "当前版本销量 5" in answer
    assert "整个商品系列累计销量 12" in answer
    assert "SKU" not in answer
    assert "SPU" not in answer


def test_order_fallback_distinguishes_exact_total_from_returned_window() -> None:
    state = cast(
        AgentState,
        {
            "parsed": {
                "order_candidates": [
                    {
                        "id": 202607020001 + index,
                        "status_label": "已发货",
                        "pay_amount": "99.00",
                    }
                    for index in range(5)
                ],
                "order_query": {
                    "query_mode": "recent",
                    "total_match_count": 8,
                    "returned_count": 5,
                    "is_exhaustive": False,
                    "offset": 0,
                    "next_offset": 5,
                },
            }
        },
    )

    answer = _fallback_answer(state)

    assert "一共有 8 个订单" in answer
    assert "最近的 5 个" in answer
    assert "只是部分结果" in answer
    assert "只有 5 个订单" not in answer


def test_order_count_fallback_answers_zero_as_reliable_fact() -> None:
    state = cast(
        AgentState,
        {
            "parsed": {
                "order_candidates": [],
                "order_query": {
                    "query_mode": "count",
                    "total_match_count": 0,
                    "returned_count": 0,
                    "is_exhaustive": True,
                    "offset": 0,
                },
            }
        },
    )

    assert _fallback_answer(state) == "你当前一共有 0 个订单。"


def test_order_page_fallback_reports_exhaustion_without_erasing_total() -> None:
    state = cast(
        AgentState,
        {
            "parsed": {
                "order_candidates": [],
                "order_query": {
                    "query_mode": "page",
                    "total_match_count": 7,
                    "returned_count": 0,
                    "is_exhaustive": True,
                    "offset": 7,
                },
            }
        },
    )

    assert _fallback_answer(state) == "已经列完全部 7 个订单，没有其他订单了。"


def test_catalog_fallback_uses_customer_language_for_applied_usage_mapping() -> None:
    state = cast(
        AgentState,
        {
            "message": "推荐办公键盘",
            "parsed": {
                "product_search": {
                    "usage_mapping": {
                        "status": "applied",
                        "source": "deterministic_spec_mapping",
                    }
                }
            },
            "products": [
                ProductCard(
                    spu_id=10,
                    sku_id=101,
                    title="Test Office Keyboard",
                    brand="Test",
                    category="keyboard",
                    price="399.00",
                    stock=8,
                    specs={"switches": "静音红轴"},
                )
            ],
        },
    )

    answer = _fallback_answer(state)

    assert "使用场景相关的规格要求和偏好" in answer
    assert "轴体: 静音红轴" in answer
    assert "switches" not in answer
    assert "告诉我主要用途" not in answer
    assert "usage_mapping" not in answer
    assert "deterministic_spec_mapping" not in answer


def test_catalog_fallback_marks_primary_recommendation_and_partial_window() -> None:
    state = cast(
        AgentState,
        {
            "message": "你最推荐哪个版本",
            "parsed": {
                "product_search": {
                    "result_purpose": "recommendation",
                    "selection_scope": "sku",
                    "total_match_count": 12,
                    "returned_count": 3,
                    "is_exhaustive": False,
                }
            },
            "products": [
                ProductCard(
                    spu_id=10,
                    sku_id=101,
                    title="首选版本",
                    brand="Test",
                    category="keyboard",
                    price="350.00",
                    stock=8,
                ),
                ProductCard(
                    spu_id=10,
                    sku_id=102,
                    title="备选版本 A",
                    brand="Test",
                    category="keyboard",
                    price="355.00",
                    stock=7,
                ),
                ProductCard(
                    spu_id=10,
                    sku_id=103,
                    title="备选版本 B",
                    brand="Test",
                    category="keyboard",
                    price="360.00",
                    stock=6,
                ),
            ],
        },
    )

    answer = _fallback_answer(state)

    assert "共匹配 12 个具体版本，本次返回 3 个候选" in answer
    assert "首选 · 首选版本" in answer
    assert "备选 · 备选版本 A" in answer
    assert "只有 3 个版本" not in answer


def test_usage_mapping_unavailable_fallback_is_qualified_no_match() -> None:
    result = _result(
        "catalog_search",
        {
            "result_type": "empty",
            "products": [],
            "ranking_strategy": "unsupported_query",
            "query_plan": {
                "supported": False,
                "error_type": "usage_mapping_unavailable",
            },
            "diagnostics": [{"code": "usage_mapping_unavailable"}],
        },
    )
    state = cast(
        AgentState,
        {
            "tool_results": [result],
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "call-1",
                            "name": "catalog_search",
                            "arguments": {"query": "办公鼠标"},
                            "subquery": "推荐办公鼠标",
                        }
                    ],
                    "results": [result],
                }
            ],
            "subquery_ledger": [
                entry.model_dump(mode="json")
                for entry in build_subquery_ledger(
                    [
                        {
                            "wave": 1,
                            "calls": [
                                {
                                    "id": "call-1",
                                    "name": "catalog_search",
                                    "arguments": {"query": "办公鼠标"},
                                    "subquery": "推荐办公鼠标",
                                }
                            ],
                            "results": [result],
                        }
                    ]
                )
            ],
        },
    )

    answer = _fallback_unavailable_answer(state)

    assert "没有找到能够依据现有规格确认适合这个使用场景的商品" in answer
    assert "没有该品类商品" not in answer
    assert "系统" not in answer
    assert "usage_mapping" not in answer


def test_duplicate_tool_call_ids_are_normalized_before_ledger_execution() -> None:
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    decision = OrchestratorDecision(
        type="tool_calls",
        tool_calls=[
            PlannedToolCall(
                id="reused",
                name="catalog_search",
                arguments={"query": "鼠标"},
                subquery="sq_1",
            ),
            PlannedToolCall(
                id="reused",
                name="policy_search",
                arguments={"query": "退货"},
                subquery="sq_2",
            ),
        ],
    )
    state = cast(
        AgentState,
        {
            "message": "查询鼠标并说明退货政策",
            "route_plan": {
                "rewritten_query": "查询鼠标并说明退货政策",
                "subqueries": [
                    {
                        "id": "sq_1",
                        "query": "查询鼠标",
                        "disposition": "tool_planning",
                        "reason_code": "catalog_read",
                    },
                    {
                        "id": "sq_2",
                        "query": "说明退货政策",
                        "disposition": "tool_planning",
                        "reason_code": "policy_read",
                    },
                ],
            },
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [{"id": "reused", "name": "knowledge_search"}],
                }
            ],
            "tool_wave_count": 0,
        },
    )
    initialize_task_runtime(state)

    normalized = runtime._validate_decision_budget(state, decision, call_count=2)
    call_ids = [call.id for call in normalized.tool_calls]

    assert call_ids == ["call_2_1", "call_2_2"]
    assert len(call_ids) == len(set(call_ids))


def test_finish_answer_requires_declared_usable_tool_call_ids() -> None:
    ledger = [
        {
            "tool_call_id": "empty-1",
            "has_usable_information": False,
        },
        {
            "tool_call_id": "usable-1",
            "has_usable_information": True,
        },
    ]
    valid = OrchestratorDecision(
        type="grounded_response",
        response="有依据的回答",
        control_action="finish_answer",
        used_tool_call_ids=["usable-1"],
    )
    invalid = valid.model_copy(update={"used_tool_call_ids": ["empty-1"]})

    assert validate_terminal_decision(valid, ledger).valid is True
    assert validate_terminal_decision(invalid, ledger).valid is False


def test_finish_partial_requires_usable_part_and_explicit_unavailable_part() -> None:
    ledger = [{"tool_call_id": "catalog-1", "has_usable_information": True}]
    decision = OrchestratorDecision(
        type="partial_response",
        response="鼠标已找到；天气不在服务范围。",
        control_action="finish_partial",
        used_tool_call_ids=["catalog-1"],
        unavailable_parts=["天气"],
    )

    assert validate_terminal_decision(decision, ledger).valid is True
    assert (
        validate_terminal_decision(
            decision.model_copy(update={"unavailable_parts": []}), ledger
        ).valid
        is False
    )


def test_finish_unavailable_requires_tool_results_and_zero_usable_information() -> None:
    decision = OrchestratorDecision(
        type="unavailable_response",
        response="没有查到",
        control_action="finish_unavailable",
        unavailable_parts=["商品信息"],
    )
    empty_ledger = [{"tool_call_id": "empty-1", "has_usable_information": False}]
    usable_ledger = [{"tool_call_id": "usable-1", "has_usable_information": True}]

    assert validate_terminal_decision(decision, empty_ledger).valid is True
    assert validate_terminal_decision(decision, []).valid is False
    assert validate_terminal_decision(decision, usable_ledger).valid is False


def test_finish_unavailable_allows_only_validated_observation_coverage_failure() -> None:
    decision = OrchestratorDecision(
        type="unavailable_response",
        response="订单结果没有包含收货地址。",
        control_action="finish_unavailable",
        unavailable_parts=["所有订单的收货地址"],
        observation_coverage_failure=True,
    )
    usable_ledger = [{"tool_call_id": "order-1", "has_usable_information": True}]

    assert validate_terminal_decision(decision, usable_ledger).valid is False
    assert (
        validate_terminal_decision(
            decision,
            usable_ledger,
            observation_coverage_failure_allowed=True,
        ).valid
        is True
    )
    assert (
        validate_terminal_decision(
            decision.model_copy(update={"used_tool_call_ids": ["order-1"]}),
            usable_ledger,
            observation_coverage_failure_allowed=True,
        ).valid
        is False
    )


def test_finish_unavailable_parses_structured_handoff_confirmation_offer() -> None:
    message = _control_message(
        "finish_unavailable",
        response="目前不能直接办理这项操作。",
        unavailable_parts=["办理退货"],
        offer_handoff_confirmation=True,
        observation_coverage_failure=True,
    )

    decision = decision_from_ai_message(message)

    assert decision.control_action == "finish_unavailable"
    assert decision.offer_handoff_confirmation is True
    assert decision.observation_coverage_failure is True


def test_control_action_cannot_be_mixed_with_business_tool_call() -> None:
    message = _control_message("finish_answer", response="你好", used_tool_call_ids=["catalog-1"])
    message.tool_calls.append(
        {
            "id": "catalog-1",
            "name": "catalog_search",
            "args": {"query": "鼠标"},
            "type": "tool_call",
        }
    )

    with pytest.raises(ValueError, match="cannot be mixed"):
        decision_from_ai_message(message)


def test_plain_text_terminal_is_rejected_for_guarded_replanning() -> None:
    with pytest.raises(ValueError, match="plain text instead of a control action"):
        decision_from_ai_message(AIMessage(content="我查到了三款商品。"))


class _FakeChatModel:
    def __init__(self, responses: list[AIMessage]):
        self.responses = responses
        self.call_count = 0
        self.bound_tool_sets: list[set[str]] = []

    def bind_tools(self, tools: list[dict[str, Any]], **_: Any):
        self.bound_tool_sets.append({tool["function"]["name"] for tool in tools})
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        response = self.responses[self.call_count]
        self.call_count += 1
        return response


@pytest.mark.asyncio
async def test_guard_replans_false_grounded_answer_then_renderer_uses_unavailable() -> None:
    model = _FakeChatModel(
        [
            _control_message(
                "finish_answer",
                response="销量正在上涨。",
                used_tool_call_ids=["catalog-1"],
            ),
            _control_message(
                "finish_unavailable",
                response="销量数据显示最近三个月上涨了 20%。",
                unavailable_parts=["销量趋势"],
            ),
        ]
    )
    runtime = AgentRuntime(
        cast(Any, None),
        Settings(llm_api_key=""),
        chat_model=model,
    )
    unsupported = _result(
        "catalog_search",
        {
            "result_type": "empty",
            "products": [],
            "ranking_strategy": "unsupported_query",
            "query_plan": {"supported": False},
        },
        call_id="catalog-1",
    )
    state = cast(
        AgentState,
        {
            "message": "分析一下鼠标最近三个月的销量趋势",
            "route_plan": _tool_route_plan("分析一下鼠标最近三个月的销量趋势"),
            "history": [],
            "tool_results": [unsupported],
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "catalog-1",
                            "name": "catalog_search",
                            "arguments": {"query": "鼠标销量趋势"},
                        }
                    ],
                    "results": [unsupported],
                }
            ],
            "subquery_ledger": [
                {
                    "tool_call_id": "catalog-1",
                    "tool_name": "catalog_search",
                    "subquery": "sq_1",
                    "outcome": "unsupported",
                    "has_usable_information": False,
                    "reason": "query_not_supported_by_tool",
                    "wave": 1,
                    "arguments": {"query": "鼠标销量趋势"},
                }
            ],
            "tool_wave_count": 1,
            "orchestrator_call_count": 1,
            "terminal_guard_replan_count": 0,
        },
    )

    first = await runtime._orchestrate(state)
    first_guard = await runtime._terminal_guard(first)
    assert first_guard["terminal_guard_status"] == "replan"
    assert first_guard["terminal_guard_feedback"] == (
        "finish_answer_requires_all_initial_subqueries_resolved"
    )

    second = await runtime._orchestrate(first_guard)
    second_guard = await runtime._terminal_guard(second)
    finalized = await runtime._finalize_response(second_guard)

    assert second_guard["terminal_guard_status"] == "accepted"
    assert second_guard["decision"]["type"] == "unavailable_response"
    assert second_guard["boundary"]["classification"] == "human_handoff_required"
    assert finalized["answer"] == LATE_HANDOFF_CONFIRMATION
    assert finalized["suggested_actions"][0]["payload"]["handoff"] is True
    assert "上涨" not in finalized["answer"]
