from typing import Any, cast

import pytest

from app.agent.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.graph import (
    AgentRuntime,
    _followup_tool_call_allowed,
    _state_terminal_decision,
)
from app.agent.outcomes import build_subquery_ledger, normalize_tool_result
from app.agent.projections import rebuild_tool_projections
from app.agent.state import AgentState
from app.core.config import Settings


def _product(sku_id: int = 101, *, brand: str = "Wooting") -> dict[str, Any]:
    return {
        "spu_id": 10,
        "sku_id": sku_id,
        "title": f"{brand} Keyboard {sku_id}",
        "brand": brand,
        "category": "keyboard",
        "price": "699.00",
        "stock": 8,
        "sku_sales_count": 3,
        "sales_count": 12,
        "specs": {"connection_type": "Wireless"},
    }


def _document() -> dict[str, Any]:
    return {
        "source_type": "knowledge_document",
        "source_id": 1,
        "title": "DPI 指南",
        "document_type": "guide",
        "snippet": "办公可选择较低 DPI。",
    }


def _result(
    call_id: str,
    name: str,
    *,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    execution: dict[str, Any]
    if error is None:
        execution = {"tool_name": name, "ok": True, "output": output, "error": None}
    else:
        execution = {"tool_name": name, "ok": False, "output": None, "error": error}
    return {"tool_call_id": call_id, "name": name, "execution": execution}


def _wave(
    number: int,
    call_id: str,
    name: str,
    query: str,
    subquery: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "wave": number,
        "calls": [
            {
                "id": call_id,
                "name": name,
                "arguments": {"query": query},
                "subquery": subquery,
            }
        ],
        "results": [result],
    }


def _state_from_waves(waves: list[dict[str, Any]]) -> AgentState:
    results = [result for wave in waves for result in wave["results"]]
    state = cast(
        AgentState,
        {
            "message": "用途为办公推荐几个键盘",
            "tool_waves": waves,
            "tool_results": results,
            "tool_wave_count": len(waves),
            "subquery_ledger": [
                entry.model_dump(mode="json") for entry in build_subquery_ledger(waves)
            ],
            "parsed": {},
            "products": [],
            "evidence": [],
            "order": None,
        },
    )
    rebuild_tool_projections(state)
    return state


def test_unsupported_catalog_result_does_not_clear_working_memory_candidates() -> None:
    state = _state_from_waves(
        [
            _wave(
                1,
                "catalog-unsupported",
                "catalog_search",
                "查询键盘销量第二",
                "sq_1",
                _result(
                    "catalog-unsupported",
                    "catalog_search",
                    output={
                        "result_type": "empty",
                        "products": [],
                        "diagnostics": [
                            {
                                "code": "unsupported_query",
                                "severity": "error",
                                "message": "unsupported",
                            }
                        ],
                    },
                ),
            )
        ]
    )

    assert state["catalog_tool_succeeded"] is False


@pytest.mark.parametrize(
    ("case_result", "expected_outcome", "expected_usable"),
    [
        pytest.param(
            _result(
                "catalog-one",
                "catalog_search",
                output={"result_type": "products", "products": [_product()]},
            ),
            "usable",
            True,
            id="01-one-catalog-product-is-usable",
        ),
        pytest.param(
            _result(
                "catalog-empty",
                "catalog_search",
                output={"result_type": "empty", "products": []},
            ),
            "empty",
            False,
            id="02-empty-catalog-result",
        ),
        pytest.param(
            _result(
                "catalog-unsupported",
                "catalog_search",
                output={
                    "result_type": "empty",
                    "products": [],
                    "diagnostics": [{"code": "unsupported_query"}],
                },
            ),
            "unsupported",
            False,
            id="03-unsupported-catalog-query",
        ),
        pytest.param(
            _result(
                "catalog-invalid-plan",
                "catalog_search",
                output={
                    "result_type": "empty",
                    "products": [],
                    "diagnostics": [{"code": "invalid_catalog_plan"}],
                },
            ),
            "insufficient",
            False,
            id="04-invalid-catalog-plan",
        ),
        pytest.param(
            _result(
                "compare-two",
                "catalog_compare",
                output={
                    "result_type": "comparison",
                    "products": [_product(101), _product(102, brand="Keychron")],
                },
            ),
            "usable",
            True,
            id="05-two-product-comparison",
        ),
        pytest.param(
            _result(
                "compare-one",
                "catalog_compare",
                output={"result_type": "comparison", "products": [_product()]},
            ),
            "insufficient",
            False,
            id="06-one-product-comparison",
        ),
        pytest.param(
            _result(
                "facet-one",
                "catalog_facets",
                output={"result_type": "facets", "items": [{"value": "Wooting"}]},
            ),
            "usable",
            True,
            id="07-one-brand-facet-is-enough",
        ),
        pytest.param(
            _result(
                "order-missing",
                "order_lookup",
                output={"result_type": "not_found", "candidates": []},
            ),
            "not_found",
            False,
            id="08-order-not-found",
        ),
        pytest.param(
            _result(
                "knowledge-one",
                "knowledge_search",
                output={"result_type": "documents", "documents": [_document()]},
            ),
            "usable",
            True,
            id="09-one-document-is-usable",
        ),
        pytest.param(
            _result(
                "catalog-timeout",
                "catalog_search",
                error={
                    "code": "timeout",
                    "retryable": True,
                    "recommended_action": "retry_once",
                },
            ),
            "error",
            False,
            id="10-tool-timeout",
        ),
    ],
)
def test_wave_loop_outcome_cases(
    case_result: dict[str, Any],
    expected_outcome: str,
    expected_usable: bool,
) -> None:
    outcome = normalize_tool_result(case_result)

    assert outcome.outcome == expected_outcome
    assert outcome.has_usable_information is expected_usable


@pytest.mark.parametrize(
    ("waves", "next_call", "expected_allowed"),
    [
        pytest.param(
            [
                _wave(
                    1,
                    "office",
                    "catalog_search",
                    "办公键盘",
                    "推荐办公键盘",
                    _result(
                        "office",
                        "catalog_search",
                        output={"result_type": "products", "products": [_product()]},
                    ),
                )
            ],
            PlannedToolCall(
                id="more-brands",
                name="catalog_search",
                arguments={"query": "Keychron Akko 办公键盘"},
                subquery="推荐其他品牌的办公键盘",
            ),
            False,
            id="11-do-not-invent-more-brands",
        ),
        pytest.param(
            [
                _wave(
                    1,
                    "office",
                    "catalog_search",
                    "办公键盘",
                    "推荐办公键盘",
                    _result(
                        "office",
                        "catalog_search",
                        output={"result_type": "products", "products": [_product()]},
                    ),
                )
            ],
            PlannedToolCall(
                id="repeat",
                name="catalog_search",
                arguments={"query": "办公键盘"},
                subquery="推荐办公键盘",
            ),
            False,
            id="12-do-not-repeat-usable-subquery",
        ),
        pytest.param(
            [
                _wave(
                    1,
                    "timeout-1",
                    "catalog_search",
                    "办公键盘",
                    "推荐办公键盘",
                    _result(
                        "timeout-1",
                        "catalog_search",
                        error={
                            "code": "timeout",
                            "retryable": True,
                            "recommended_action": "retry_once",
                        },
                    ),
                )
            ],
            PlannedToolCall(
                id="timeout-2",
                name="catalog_search",
                arguments={"query": "办公键盘"},
                subquery="推荐办公键盘",
            ),
            True,
            id="13-allow-first-timeout-retry",
        ),
        pytest.param(
            [
                _wave(
                    1,
                    "timeout-1",
                    "catalog_search",
                    "办公键盘",
                    "推荐办公键盘",
                    _result(
                        "timeout-1",
                        "catalog_search",
                        error={
                            "code": "timeout",
                            "retryable": True,
                            "recommended_action": "retry_once",
                        },
                    ),
                ),
                _wave(
                    2,
                    "timeout-2",
                    "catalog_search",
                    "办公键盘",
                    "推荐办公键盘",
                    _result(
                        "timeout-2",
                        "catalog_search",
                        error={
                            "code": "timeout",
                            "retryable": True,
                            "recommended_action": "retry_once",
                        },
                    ),
                ),
            ],
            PlannedToolCall(
                id="timeout-3",
                name="catalog_search",
                arguments={"query": "办公键盘"},
                subquery="推荐办公键盘",
            ),
            False,
            id="14-block-second-timeout-retry",
        ),
        pytest.param(
            [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "invalid-1",
                            "name": "catalog_search",
                            "arguments": {"query": "办公键盘", "limit": 0},
                            "subquery": "推荐办公键盘",
                        }
                    ],
                    "results": [
                        _result(
                            "invalid-1",
                            "catalog_search",
                            error={
                                "code": "invalid_input",
                                "retryable": True,
                                "recommended_action": "replan_arguments",
                            },
                        )
                    ],
                }
            ],
            PlannedToolCall(
                id="invalid-2",
                name="catalog_search",
                arguments={"query": "办公键盘", "limit": 3},
                subquery="推荐办公键盘",
            ),
            True,
            id="15-allow-corrected-non-query-input",
        ),
        pytest.param(
            [
                _wave(
                    1,
                    "internal-error",
                    "catalog_search",
                    "办公键盘",
                    "推荐办公键盘",
                    _result(
                        "internal-error",
                        "catalog_search",
                        error={
                            "code": "execution_error",
                            "retryable": False,
                            "recommended_action": "stop",
                        },
                    ),
                )
            ],
            PlannedToolCall(
                id="retry-internal",
                name="catalog_search",
                arguments={"query": "办公键盘"},
                subquery="推荐办公键盘",
            ),
            False,
            id="16-block-stop-error-retry",
        ),
        pytest.param(
            [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "invalid-1",
                            "name": "catalog_search",
                            "arguments": {"query": "办公键盘", "limit": 0},
                            "subquery": "推荐办公键盘",
                        }
                    ],
                    "results": [
                        _result(
                            "invalid-1",
                            "catalog_search",
                            error={
                                "code": "invalid_input",
                                "retryable": True,
                                "recommended_action": "replan_arguments",
                            },
                        )
                    ],
                }
            ],
            PlannedToolCall(
                id="switch-tool",
                name="knowledge_search",
                arguments={"query": "办公键盘"},
                subquery="推荐办公键盘",
            ),
            False,
            id="17-invalid-input-cannot-switch-tools",
        ),
    ],
)
def test_wave_loop_followup_policy_cases(
    waves: list[dict[str, Any]],
    next_call: PlannedToolCall,
    expected_allowed: bool,
) -> None:
    state = _state_from_waves(waves)

    assert _followup_tool_call_allowed(state, next_call) is expected_allowed


def test_legacy_dependent_compare_without_a_ready_routed_task_is_blocked() -> None:
    result = _result(
        "search-1",
        "catalog_search",
        output={
            "result_type": "products",
            "products": [_product(101), _product(102, brand="Keychron")],
        },
    )
    state = _state_from_waves(
        [
            _wave(
                1,
                "search-1",
                "catalog_search",
                "办公键盘",
                "查找办公键盘候选",
                result,
            )
        ]
    )
    call = PlannedToolCall(
        id="compare-1",
        name="catalog_compare",
        arguments={"query": "比较这两个具体 SKU 办公键盘版本", "limit": 5},
        subquery="比较候选办公键盘",
    )

    state["message"] = "先推荐办公键盘，再比较候选商品的区别"
    assert _followup_tool_call_allowed(state, call) is False

    state["message"] = "Find office keyboards and compare the returned products"
    assert _followup_tool_call_allowed(state, call) is False

    state["message"] = "推荐办公键盘"
    assert _followup_tool_call_allowed(state, call) is False

    state["message"] = "先推荐办公键盘，再比较候选商品的区别"
    unbound_compare_call = call.model_copy(
        update={"arguments": {"query": "比较其他两个具体 SKU 候选版本", "limit": 5}}
    )
    assert _followup_tool_call_allowed(state, unbound_compare_call) is False


def test_compare_cannot_reuse_a_completed_search_task_as_implicit_authorization() -> None:
    query = "查找销量最高的两款显示器并进行对比"
    result = _result(
        "search-1",
        "catalog_search",
        output={
            "result_type": "products",
            "products": [_product(101), _product(102, brand="Keychron")],
        },
    )
    state = _state_from_waves([_wave(1, "search-1", "catalog_search", query, "sq_1", result)])
    state["message"] = "比较两个销量最高的显示器"
    call = PlannedToolCall(
        id="compare-1",
        name="catalog_compare",
        arguments={"query": query, "limit": 2},
        subquery="sq_1",
    )

    assert _followup_tool_call_allowed(state, call) is False


def test_dependent_order_lookup_requires_a_returned_candidate_id() -> None:
    result = _result(
        "orders-1",
        "order_lookup",
        output={
            "result_type": "order_candidates",
            "candidates": [{"id": 202607210001}, {"id": 202607210002}],
        },
    )
    state = _state_from_waves(
        [
            _wave(
                1,
                "orders-1",
                "order_lookup",
                "查询最近订单",
                "查询最近订单候选",
                result,
            )
        ]
    )
    valid_call = PlannedToolCall(
        id="order-detail",
        name="order_lookup",
        arguments={"order_id": 202607210001, "limit": 1},
        subquery="读取最近一笔订单详情",
    )
    invalid_call = valid_call.model_copy(
        update={"arguments": {"order_id": 202607219999, "limit": 1}}
    )

    assert _followup_tool_call_allowed(state, valid_call) is True
    assert _followup_tool_call_allowed(state, invalid_call) is False


def test_timeout_retry_cannot_rewrite_canonical_query() -> None:
    timeout = _result(
        "timeout-1",
        "catalog_search",
        error={
            "code": "timeout",
            "retryable": True,
            "recommended_action": "retry_once",
        },
    )
    state = _state_from_waves(
        [
            _wave(
                1,
                "timeout-1",
                "catalog_search",
                "办公键盘",
                "推荐办公键盘",
                timeout,
            )
        ]
    )
    rewritten = PlannedToolCall(
        id="timeout-2",
        name="catalog_search",
        arguments={"query": "适合办公使用的无线机械键盘"},
        subquery="推荐办公键盘",
    )

    assert _followup_tool_call_allowed(state, rewritten) is False


def test_timeout_retry_allows_only_normalized_equivalent_query() -> None:
    timeout = _result(
        "timeout-1",
        "catalog_search",
        error={
            "code": "timeout",
            "retryable": True,
            "recommended_action": "retry_once",
        },
    )
    state = _state_from_waves(
        [
            _wave(
                1,
                "timeout-1",
                "catalog_search",
                "Office   Keyboard",
                "推荐办公键盘",
                timeout,
            )
        ]
    )
    retry = PlannedToolCall(
        id="timeout-2",
        name="catalog_search",
        arguments={"query": "  office keyboard  "},
        subquery="推荐办公键盘",
    )

    assert _followup_tool_call_allowed(state, retry) is True


def test_insufficient_result_cannot_be_retried_with_rewritten_query() -> None:
    insufficient = _result(
        "plan-failed",
        "catalog_search",
        output={
            "result_type": "empty",
            "products": [],
            "diagnostics": [{"code": "invalid_catalog_plan"}],
        },
    )
    state = _state_from_waves(
        [
            _wave(
                1,
                "plan-failed",
                "catalog_search",
                "办公键盘",
                "推荐办公键盘",
                insufficient,
            )
        ]
    )
    retry = PlannedToolCall(
        id="plan-retry",
        name="catalog_search",
        arguments={"query": "静音无线办公键盘"},
        subquery="推荐办公键盘",
    )

    assert _followup_tool_call_allowed(state, retry) is False


def test_runtime_allows_invalid_input_fix_when_query_is_unchanged() -> None:
    invalid = _result(
        "invalid-1",
        "catalog_search",
        error={
            "code": "invalid_input",
            "retryable": True,
            "recommended_action": "replan_arguments",
        },
    )
    state = _state_from_waves(
        [
            {
                "wave": 1,
                "calls": [
                    {
                        "id": "invalid-1",
                        "name": "catalog_search",
                        "arguments": {"query": "办公键盘", "limit": 0},
                        "subquery": "推荐办公键盘",
                    }
                ],
                "results": [invalid],
            }
        ]
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    proposed = OrchestratorDecision(
        type="tool_calls",
        tool_calls=[
            PlannedToolCall(
                id="invalid-2",
                name="catalog_search",
                arguments={"query": "办公键盘", "limit": 3},
                subquery="推荐办公键盘",
            )
        ],
    )

    decision = runtime._validate_decision_budget(state, proposed, call_count=2)

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].arguments["query"] == "办公键盘"
    assert decision.tool_calls[0].arguments["limit"] == 3


def test_runtime_terminates_invalid_input_fix_that_rewrites_query() -> None:
    invalid = _result(
        "invalid-1",
        "catalog_search",
        error={
            "code": "invalid_input",
            "retryable": True,
            "recommended_action": "replan_arguments",
        },
    )
    state = _state_from_waves(
        [
            {
                "wave": 1,
                "calls": [
                    {
                        "id": "invalid-1",
                        "name": "catalog_search",
                        "arguments": {"query": "办公键盘", "limit": 0},
                        "subquery": "推荐办公键盘",
                    }
                ],
                "results": [invalid],
            }
        ]
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    proposed = OrchestratorDecision(
        type="tool_calls",
        tool_calls=[
            PlannedToolCall(
                id="invalid-2",
                name="catalog_search",
                arguments={"query": "静音无线办公键盘", "limit": 3},
                subquery="推荐办公键盘",
            )
        ],
    )

    decision = runtime._validate_decision_budget(state, proposed, call_count=2)

    assert decision.type == "unavailable_response"
    assert decision.tool_calls == []


@pytest.mark.parametrize(
    ("waves", "expected_type", "expected_products"),
    [
        pytest.param(
            [
                _wave(
                    1,
                    "usable",
                    "catalog_search",
                    "办公键盘",
                    "推荐办公键盘",
                    _result(
                        "usable",
                        "catalog_search",
                        output={"result_type": "products", "products": [_product()]},
                    ),
                )
            ],
            "grounded_response",
            1,
            id="17-limit-preserves-usable-result",
        ),
        pytest.param(
            [
                _wave(
                    1,
                    "old-usable",
                    "catalog_search",
                    "500元内办公键盘",
                    "推荐办公键盘",
                    _result(
                        "old-usable",
                        "catalog_search",
                        output={"result_type": "products", "products": [_product()]},
                    ),
                ),
                _wave(
                    2,
                    "replacement-empty",
                    "catalog_search",
                    "300元内办公键盘",
                    "推荐办公键盘",
                    _result(
                        "replacement-empty",
                        "catalog_search",
                        output={"result_type": "empty", "products": []},
                    ),
                ),
            ],
            "unavailable_response",
            0,
            id="19-superseded-result-is-not-citable",
        ),
    ],
)
def test_wave_loop_terminal_cases(
    waves: list[dict[str, Any]],
    expected_type: str,
    expected_products: int,
) -> None:
    state = _state_from_waves(waves)
    decision = _state_terminal_decision(state, "orchestration_limit_reached")

    assert decision.type == expected_type
    assert len(state["products"]) == expected_products


class _FailingChatModel:
    def bind_tools(self, tools: list[dict[str, Any]], **_: Any) -> "_FailingChatModel":
        return self

    async def ainvoke(self, messages: list[Any]) -> None:
        raise RuntimeError("provider unavailable")


@pytest.mark.asyncio
async def test_20_orchestrator_failure_after_tool_success_uses_grounded_fallback() -> None:
    result = _result(
        "usable",
        "catalog_search",
        output={"result_type": "products", "products": [_product()]},
    )
    state = _state_from_waves([_wave(1, "usable", "catalog_search", "办公键盘", "sq_1", result)])
    state.update(
        {
            "route_plan": {
                "rewritten_query": "推荐办公键盘",
                "subqueries": [
                    {
                        "id": "sq_1",
                        "query": "推荐办公键盘",
                        "disposition": "tool_planning",
                        "reason_code": "catalog_read",
                    }
                ],
            },
            "history": [],
            "orchestrator_call_count": 1,
            "terminal_guard_replan_count": 0,
        }
    )
    runtime = AgentRuntime(
        cast(Any, None),
        Settings(llm_api_key=""),
        chat_model=_FailingChatModel(),
    )

    updated = await runtime._orchestrate(state)

    assert updated["decision"]["type"] == "grounded_response"
    assert updated["decision"]["used_tool_call_ids"] == ["usable"]
    assert "Wooting Keyboard" in updated["decision"]["response"]


def test_office_keyboard_more_brand_wave_is_terminated_with_existing_result() -> None:
    first_result = _result(
        "office",
        "catalog_search",
        output={"result_type": "products", "products": [_product()]},
    )
    state = _state_from_waves(
        [
            _wave(
                1,
                "office",
                "catalog_search",
                "办公键盘",
                "推荐办公键盘",
                first_result,
            )
        ]
    )
    runtime = AgentRuntime(cast(Any, None), Settings(llm_api_key=""))
    proposed = OrchestratorDecision(
        type="tool_calls",
        tool_calls=[
            PlannedToolCall(
                id="more-brands",
                name="catalog_search",
                arguments={"query": "Keychron Akko 办公键盘"},
                subquery="推荐其他品牌的办公键盘",
            )
        ],
    )

    decision = runtime._validate_decision_budget(state, proposed, call_count=2)

    assert decision.type == "grounded_response"
    assert decision.tool_calls == []
    assert decision.used_tool_call_ids == ["office"]
    assert "Wooting Keyboard" in decision.response


def test_later_timeout_cannot_replace_first_wave_usable_result_at_limit() -> None:
    usable = _result(
        "office",
        "catalog_search",
        output={"result_type": "products", "products": [_product()]},
    )
    timeout = _result(
        "more-brands",
        "catalog_search",
        error={
            "code": "timeout",
            "retryable": True,
            "recommended_action": "retry_once",
        },
    )
    state = _state_from_waves(
        [
            _wave(
                1,
                "office",
                "catalog_search",
                "办公键盘",
                "推荐办公键盘",
                usable,
            ),
            _wave(
                2,
                "more-brands",
                "catalog_search",
                "Keychron Akko 办公键盘",
                "推荐其他品牌的办公键盘",
                timeout,
            ),
        ]
    )

    decision = _state_terminal_decision(state, "orchestration_limit_reached")

    assert decision.type == "grounded_response"
    assert decision.used_tool_call_ids == ["office"]
    assert "处理上限" not in decision.response
    assert "Wooting Keyboard" in decision.response
