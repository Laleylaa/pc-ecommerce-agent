from copy import deepcopy
from typing import Any, cast

import pytest

from app.agent.artifacts import ensure_task_runtime
from app.agent.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.graph import (
    AgentRuntime,
    _followup_tool_call_allowed,
    _state_terminal_decision,
)
from app.agent.outcomes import build_subquery_ledger
from app.agent.projections import rebuild_tool_projections
from app.agent.state import AgentState
from app.core.config import Settings
from app.schemas.catalog_search import CatalogSearchResult
from app.tools.catalog_search.outcomes import policy_for


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


def _answer_state_from_waves(waves: list[dict[str, Any]]) -> AgentState:
    """Current task-addressed answer fixture; the legacy policy cases stay independent."""
    waves = deepcopy(waves)
    task_ids = {}
    tasks = {}
    for wave in waves:
        for call in wave["calls"]:
            original = call["subquery"]
            identity = task_ids.setdefault(original, f"task_{len(task_ids) + 1}")
            call["subquery"] = identity
            result = next(r for r in wave["results"] if r["tool_call_id"] == call["id"])
            execution = result["execution"]
            if call["name"] == "catalog_search" and execution["ok"]:
                output = execution["output"]
                count = len(output["products"])
                for product in output["products"]:
                    product.update(candidate_sku_ids=[product["sku_id"]], model_sku_count=1)
                reason = "matches_found" if count else "no_matching_sku"
                output.update(
                    **policy_for(reason).fields(), query_state="completed",
                    match_kind="exact" if count else "none",
                    selection_scope="sku", returned_count=count, offset=0, limit=3,
                    total_match_count=count, candidate_count=count,
                    candidate_spu_count=count, candidate_sku_count=count,
                    matched_spu_count=count, matched_sku_count=count,
                    unknown_sku_count=0, conflicted_sku_count=0, missing_fact_sku_count=0,
                    matching_complete=True, is_exhaustive=True, data_quality="clear",
                )
                execution["output"] = CatalogSearchResult.model_validate(output).model_dump(
                    mode="json",
                )
            tasks[identity] = {
                "id": identity, "goal_id": "goal_1",
                "canonical_query": call["arguments"].get("query") or original,
                "capability": call["name"], "produces": "result", "answer_role": "user_facing",
            }
    state = _state_from_waves(waves)
    state["route_plan"] = {
        "rewritten_query": state["message"], "subqueries": [{
            "id": "goal_1", "query": state["message"], "disposition": "tool_planning",
            "reason_code": "test", "tasks": list(tasks.values()),
        }],
    }
    ensure_task_runtime(state)
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
            "partial_response",
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
            "grounded_response",
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
    state = _answer_state_from_waves(waves)
    decision = _state_terminal_decision(state, "orchestration_limit_reached")

    assert decision.type == expected_type
    assert len(state["products"]) == expected_products
    if waves[-1]["calls"][0]["id"] == "replacement-empty":
        assert "old-usable" not in decision.used_tool_call_ids
        assert decision.used_tool_call_ids == ["replacement-empty"]
        assert "没有找到" in decision.response


class _FailingChatModel:
    def bind_tools(self, tools: list[dict[str, Any]], **_: Any) -> "_FailingChatModel":
        return self

    async def ainvoke(self, messages: list[Any]) -> None:
        raise RuntimeError("provider unavailable")


@pytest.mark.asyncio
async def test_answer_failure_after_tool_success_preserves_facts_in_partial_fallback() -> None:
    result = _result(
        "usable",
        "catalog_search",
        output={"result_type": "products", "products": [_product()]},
    )
    state = _answer_state_from_waves([
        _wave(1, "usable", "catalog_search", "办公键盘", "sq_1", result),
    ])
    state.update({"history": [], "orchestrator_call_count": 1})
    runtime = AgentRuntime(
        cast(Any, None),
        Settings(llm_api_key=""),
        chat_model=_FailingChatModel(),
    )

    updated = await runtime._orchestrate(state)

    assert updated["decision"]["type"] == "partial_response"
    assert updated["decision"]["used_tool_call_ids"] == ["usable"]
    assert "Wooting Keyboard" in updated["decision"]["response"]


def test_office_keyboard_more_brand_wave_is_terminated_with_existing_result() -> None:
    first_result = _result(
        "office",
        "catalog_search",
        output={"result_type": "products", "products": [_product()]},
    )
    state = _answer_state_from_waves(
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

    assert decision.type == "partial_response"
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
    state = _answer_state_from_waves(
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

    assert decision.type == "partial_response"
    assert decision.unavailable_parts
    assert decision.used_tool_call_ids == ["office"]
    assert "处理上限" not in decision.response
    assert "Wooting Keyboard" in decision.response
