from typing import cast

import pytest
from app.agent.legacy.artifacts import initialize_task_runtime
from app.agent.legacy.decisions import decision_from_ai_message
from app.agent.legacy.prompts import (
    BASE_CUSTOMER_VOICE,
    BUSINESS_RESULT_RESPONSE_POLICY,
    CUSTOMER_RESPONSE_POLICY,
    ORCHESTRATOR_OBSERVATION_PROMPT,
    ORCHESTRATOR_PLANNING_PROMPT,
    ORCHESTRATOR_SYSTEM_PROMPT,
    REQUEST_ROUTER_SYSTEM_PROMPT,
    SECURITY_AND_PRIVACY_POLICY,
    TOOL_CALL_PROTOCOL,
    TOOL_INPUT_PROTOCOL,
    TOOL_RECOVERY_PROTOCOL,
    TOOL_SELECTION_RULES,
    build_orchestrator_system_prompt,
    build_orchestrator_user_prompt,
    build_tool_failure_prompt,
)
from app.agent.legacy.runtime import (
    AgentRuntime,
    _fallback_answer,
    _tag_from_decision,
)
from app.agent.legacy.state import AgentState
from app.core.config import Settings
from app.tools.contracts import (
    LLM_SAFE_TOOL_NAMES,
)
from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph

from tests.support.orchestration_orchestrator import (
    FakeChatModel,
    RecordingBindableModel,
    _control_message,
    _tool_route_plan,
)


def test_route_path_uses_phase_specific_tool_bindings() -> None:
    model = RecordingBindableModel()
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=model,
    )
    route_plan = {
        "rewritten_query": "推荐无线鼠标",
        "subqueries": [
            {
                "id": "sq_1",
                "query": "推荐无线鼠标",
                "disposition": "tool_planning",
                "reason_code": "catalog_read",
            }
        ],
    }
    initial = cast(
        AgentState,
        {
            "route_plan": route_plan,
            "tool_waves": [],
            "tool_results": [],
            "tool_wave_count": 0,
        },
    )
    initialize_task_runtime(initial)
    observed = cast(
        AgentState,
        {
            **initial,
            "tool_waves": [{"wave": 1}],
            "tool_wave_count": 1,
            "task_status": {
                "sq_1": {
                    "task_id": "sq_1",
                    "goal_id": "sq_1",
                    "answer_role": "user_facing",
                    "status": "succeeded",
                }
            },
            "task_artifacts": {
                "sq_1": {
                    "task_id": "sq_1",
                    "goal_id": "sq_1",
                    "artifact_type": "products",
                    "usable": True,
                    "value": {"products": [{"title": "Test Mouse"}]},
                    "evidence": [{"source_tool_call_id": "call-1"}],
                    "source_tool_call_id": "call-1",
                    "source_tool_name": "catalog_search",
                    "extractor": "deterministic",
                    "reason": "products_found",
                }
            },
            "subquery_ledger": [
                {
                    "tool_call_id": "call-1",
                    "tool_name": "catalog_search",
                    "subquery": "sq_1",
                    "status": "ready_to_answer",
                    "outcome": "usable",
                    "has_usable_information": True,
                }
            ],
        },
    )
    failed = cast(
        AgentState,
        {
            **observed,
            "task_artifacts": {},
            "task_status": {
                "sq_1": {
                    "task_id": "sq_1",
                    "goal_id": "sq_1",
                    "answer_role": "user_facing",
                    "status": "failed",
                }
            },
            "subquery_ledger": [
                {
                    "tool_call_id": "call-1",
                    "tool_name": "catalog_search",
                    "subquery": "sq_1",
                    "status": "failed",
                    "outcome": "error",
                    "has_usable_information": False,
                }
            ],
        },
    )

    business_names = set(LLM_SAFE_TOOL_NAMES)
    answer_names = {
        "finish_answer",
        "finish_partial",
        "finish_unavailable",
        "ask_clarification",
        "request_recovery",
    }
    assert runtime._orchestrator_model_for_state(initial).tool_names == business_names
    observed_model = runtime._orchestrator_model_for_state(observed)
    assert observed_model.tool_names == {"finish_answer"}
    assert observed_model.tool_choice == "required"
    assert runtime.answer_synthesizer.tool_names == answer_names
    assert runtime.answer_synthesizer.tool_choice == "required"
    assert runtime._orchestrator_model_for_state(failed).tool_names == answer_names


@pytest.mark.asyncio
async def test_answer_synthesizer_retries_business_call_then_finishes() -> None:
    model = FakeChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "invalid-repeat",
                        "name": "policy_search",
                        "args": {"query": "退货政策", "subquery": "sq_1"},
                        "type": "tool_call",
                    }
                ],
            ),
            _control_message(
                "finish_answer",
                response="退货政策以知识库说明为准。",
                used_tool_call_ids=["policy-1"],
            ),
        ]
    )
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=model,
    )
    execution = {
        "tool_name": "policy_search",
        "ok": True,
        "output": {
            "result_type": "documents",
            "documents": [{"title": "退货政策", "snippet": "以页面说明为准。"}],
        },
        "error": None,
    }
    state = cast(
        AgentState,
        {
            "message": "退货政策是什么",
            "route_plan": _tool_route_plan("退货政策是什么"),
            "tool_wave_count": 1,
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "policy-1",
                            "name": "policy_search",
                            "arguments": {"query": "退货政策是什么", "limit": 3},
                            "subquery": "sq_1",
                        }
                    ],
                    "results": [
                        {
                            "tool_call_id": "policy-1",
                            "name": "policy_search",
                            "execution": execution,
                        }
                    ],
                }
            ],
            "tool_results": [
                {
                    "tool_call_id": "policy-1",
                    "name": "policy_search",
                    "execution": execution,
                }
            ],
            "subquery_ledger": [
                {
                    "wave": 1,
                    "tool_call_id": "policy-1",
                    "tool_name": "policy_search",
                    "subquery": "sq_1",
                    "outcome": "usable",
                    "status": "ready_to_answer",
                    "has_usable_information": True,
                }
            ],
        },
    )

    decision = await runtime._invoke_orchestrator_decision(state, 1)

    assert decision.type == "grounded_response"
    assert decision.control_action == "finish_answer"
    assert model.call_count == 2


def test_orchestrator_prompt_covers_high_confusion_tool_boundaries() -> None:
    rules = "\n".join(TOOL_SELECTION_RULES)

    assert "具体 SKU" in rules
    assert "一般性的配送" in rules
    assert "catalog_search" in ORCHESTRATOR_PLANNING_PROMPT
    assert "catalog_facets" in ORCHESTRATOR_PLANNING_PROMPT
    assert "knowledge_search" in ORCHESTRATOR_PLANNING_PROMPT
    assert "order_lookup" in ORCHESTRATOR_PLANNING_PROMPT
    assert "policy_search" in ORCHESTRATOR_PLANNING_PROMPT


def test_orchestrator_prompt_defines_memory_precedence_and_fact_refresh() -> None:
    assert "working_memory" not in ORCHESTRATOR_PLANNING_PROMPT
    assert "working_memory" not in ORCHESTRATOR_OBSERVATION_PROMPT
    assert "working memory" in REQUEST_ROUTER_SYSTEM_PROMPT


def test_customer_response_prompt_hides_internal_terms_and_translates_sales_scope() -> None:
    policy = CUSTOMER_RESPONSE_POLICY

    assert "不向用户展示 Tool" in policy
    assert "不展示 spu_id、sku_id" in policy
    assert "不直接抛出 SPU、SKU" in policy
    assert "日常购物语言" in policy
    assert "当前版本销量" in policy
    assert "整个商品" in policy and "系列累计销量" in policy
    assert "不得直接输出“SKU 销量”“SPU 总销量”" in policy
    assert "语气自然、耐心" in policy
    assert BASE_CUSTOMER_VOICE in ORCHESTRATOR_OBSERVATION_PROMPT
    assert BUSINESS_RESULT_RESPONSE_POLICY in ORCHESTRATOR_OBSERVATION_PROMPT
    assert BASE_CUSTOMER_VOICE not in ORCHESTRATOR_PLANNING_PROMPT


def test_orchestrator_prompt_defines_sensitive_customer_data_boundary() -> None:
    assert "其他用户的身份与联系方式" in SECURITY_AND_PRIVACY_POLICY
    assert "不得把它误标为 out_of_scope" in SECURITY_AND_PRIVACY_POLICY
    assert "不得调用 `order_lookup`" in SECURITY_AND_PRIVACY_POLICY
    assert "固定\n  安全拒绝模板" in SECURITY_AND_PRIVACY_POLICY
    assert "商品级公开统计" in SECURITY_AND_PRIVACY_POLICY
    assert SECURITY_AND_PRIVACY_POLICY in REQUEST_ROUTER_SYSTEM_PROMPT
    assert SECURITY_AND_PRIVACY_POLICY not in ORCHESTRATOR_SYSTEM_PROMPT


def test_orchestrator_prompt_enforces_schema_fidelity_and_error_recovery() -> None:
    assert "不得翻译字段名、创造别名" in TOOL_CALL_PROTOCOL
    assert "canonical query 由 Runtime" in TOOL_INPUT_PROTOCOL
    assert "Planner 不输出" in TOOL_INPUT_PROTOCOL
    assert "不要生成或覆盖 Tool 内部查询计划" in TOOL_INPUT_PROTOCOL
    assert "code=invalid_input" in TOOL_RECOVERY_PROTOCOL
    assert "code=execution_error" in TOOL_RECOVERY_PROTOCOL
    assert "不得修改 key/value 猜测原因" in TOOL_RECOVERY_PROTOCOL
    assert "reused_from_tool_call_id" in TOOL_RECOVERY_PROTOCOL
    assert TOOL_INPUT_PROTOCOL in ORCHESTRATOR_PLANNING_PROMPT
    assert TOOL_RECOVERY_PROTOCOL not in ORCHESTRATOR_PLANNING_PROMPT
    assert TOOL_RECOVERY_PROTOCOL not in ORCHESTRATOR_OBSERVATION_PROMPT


def test_empty_tool_result_does_not_load_failure_recovery_prompt() -> None:
    tool_waves = [
        {
            "calls": [
                {
                    "id": "call-1",
                    "name": "catalog_search",
                    "arguments": {"query": "不存在的产品"},
                }
            ],
            "results": [
                {
                    "tool_call_id": "call-1",
                    "name": "catalog_search",
                    "execution": {
                        "ok": True,
                        "output": {"result_type": "empty", "products": []},
                    },
                }
            ],
        }
    ]

    assert build_tool_failure_prompt(tool_waves=tool_waves) == ""
    assert (
        build_orchestrator_system_prompt(tool_waves=tool_waves) == ORCHESTRATOR_OBSERVATION_PROMPT
    )


def test_timeout_loads_only_relevant_failure_recovery_rule() -> None:
    tool_waves = [
        {
            "calls": [
                {
                    "id": "call-1",
                    "name": "catalog_search",
                    "arguments": {"query": "无线鼠标"},
                }
            ],
            "results": [
                {
                    "tool_call_id": "call-1",
                    "name": "catalog_search",
                    "execution": {
                        "ok": False,
                        "error": {
                            "code": "timeout",
                            "retryable": True,
                            "recommended_action": "retry_once",
                        },
                    },
                }
            ],
        }
    ]

    prompt = build_tool_failure_prompt(tool_waves=tool_waves)

    assert "<tool_failure_recovery>" in prompt
    assert "`retry_once`" in prompt
    assert "`replan_arguments`" not in prompt
    assert "catalog_search：code=timeout" in prompt
    assert "当前没有成功 Tool Result" in prompt


def test_repeated_timeout_marks_retry_limit_and_mixed_success() -> None:
    timeout_result = {
        "name": "catalog_search",
        "execution": {
            "ok": False,
            "error": {
                "code": "timeout",
                "retryable": True,
                "recommended_action": "retry_once",
            },
        },
    }
    tool_waves = [
        {
            "calls": [
                {
                    "id": "call-1",
                    "name": "catalog_search",
                    "arguments": {"query": "无线鼠标"},
                },
                {
                    "id": "call-knowledge",
                    "name": "knowledge_search",
                    "arguments": {"query": "DPI"},
                },
            ],
            "results": [
                {"tool_call_id": "call-1", **timeout_result},
                {
                    "tool_call_id": "call-knowledge",
                    "name": "knowledge_search",
                    "execution": {
                        "ok": True,
                        "output": {"result_type": "documents", "documents": []},
                    },
                },
            ],
        },
        {
            "calls": [
                {
                    "id": "call-2",
                    "name": "catalog_search",
                    "arguments": {"query": "无线鼠标"},
                }
            ],
            "results": [{"tool_call_id": "call-2", **timeout_result}],
        },
    ]

    prompt = build_tool_failure_prompt(tool_waves=tool_waves)

    assert "相同调用与错误已失败 2 次，已达到重试上限" in prompt
    assert "当前没有成功 Tool Result" in prompt


def test_user_prompt_separates_request_execution_and_memory_data() -> None:
    prompt = build_orchestrator_user_prompt(
        message="换成无线",
        tool_wave_count=1,
        orchestrator_call_count=2,
        memory_context={"working_memory": {"catalog": {"category": "mouse"}}},
    )

    assert '<planner_request>\n"换成无线"\n</planner_request>' in prompt
    assert '"completed_tool_waves": 1' in prompt
    assert '"remaining_tool_waves": 1' in prompt
    assert '"remaining_orchestrator_calls": 1' in prompt
    assert '"must_terminate_now": false' in prompt
    assert "<memory_context>" in prompt


def test_user_prompt_marks_final_orchestrator_call_as_terminal_only() -> None:
    prompt = build_orchestrator_user_prompt(
        message="继续查",
        tool_wave_count=2,
        orchestrator_call_count=3,
    )

    assert '"remaining_tool_waves": 0' in prompt
    assert '"remaining_orchestrator_calls": 0' in prompt
    assert '"must_terminate_now": true' in prompt


def test_native_tool_calls_are_normalized_as_one_wave() -> None:
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "catalog-call",
                "name": "catalog_search",
                "args": {"query": "无线鼠标"},
                "type": "tool_call",
            },
            {
                "id": "policy-call",
                "name": "policy_search",
                "args": {"query": "鼠标退货政策"},
                "type": "tool_call",
            },
        ],
    )

    decision = decision_from_ai_message(message)

    assert decision.type == "tool_calls"
    assert [call.name for call in decision.tool_calls] == [
        "catalog_search",
        "policy_search",
    ]
    assert _tag_from_decision(decision, None) == "catalog_search + policy_search"


def test_catalog_facets_tag_is_preserved_across_tool_waves() -> None:
    facets = decision_from_ai_message(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "facets-call",
                    "name": "catalog_facets",
                    "args": {"query": "有哪些鼠标品牌"},
                    "type": "tool_call",
                }
            ],
        )
    )
    search = decision_from_ai_message(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "search-call",
                    "name": "catalog_search",
                    "args": {"query": "Logitech 鼠标"},
                    "type": "tool_call",
                }
            ],
        )
    )

    tag = _tag_from_decision(facets, None)

    assert _tag_from_decision(search, tag) == "catalog_facets + catalog_search"


def test_fallback_answer_renders_catalog_facets_result() -> None:
    state = cast(
        AgentState,
        {
            "tool_results": [
                {
                    "tool_call_id": "call-facets",
                    "name": "catalog_facets",
                    "execution": {
                        "ok": True,
                        "output": {
                            "result_type": "facets",
                            "facet": "brand",
                            "items": [
                                {"value": "Logitech", "count": 12},
                                {"value": "Razer", "count": 8},
                            ],
                        },
                    },
                }
            ]
        },
    )

    answer = _fallback_answer(state)

    assert "Logitech" in answer and "Razer" in answer
    # count_scope is absent: do not invent the counting unit.
    assert "12" not in answer and "8" not in answer
    assert "不代表可以任意组合" in answer


def test_finish_answer_control_action_declares_evidence_ids() -> None:
    decision = decision_from_ai_message(
        _control_message(
            "finish_answer",
            response="目前目录中有以下鼠标。",
            used_tool_call_ids=["call-1"],
        )
    )

    assert decision.type == "grounded_response"
    assert decision.used_tool_call_ids == ["call-1"]
    assert decision.response == "目前目录中有以下鼠标。"


def test_empty_terminal_response_is_rejected() -> None:
    with pytest.raises(ValueError, match="neither tool calls nor a control action"):
        decision_from_ai_message(
            AIMessage(content=""),
        )


@pytest.mark.asyncio
async def test_native_tool_call_is_parsed_from_complete_message() -> None:
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "catalog_search",
                            "args": {
                                "query": "无线鼠标",
                                "subquery": "推荐无线鼠标",
                            },
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        ),
    )
    workflow = StateGraph(AgentState)
    workflow.add_node("orchestrate", runtime._orchestrate)
    workflow.set_entry_point("orchestrate")
    workflow.add_edge("orchestrate", END)

    result = await workflow.compile().ainvoke(
        {
            "message": "推荐无线鼠标",
            "route_plan": _tool_route_plan("推荐无线鼠标"),
            "history": [],
            "tool_results": [],
            "tool_waves": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        }
    )

    decision = result["decision"]
    assert decision["type"] == "tool_calls"
    assert decision["tool_calls"][0]["name"] == "catalog_search"
    assert decision["tool_calls"][0]["subquery"] == "sq_1"
    assert "subquery" not in decision["tool_calls"][0]["arguments"]


@pytest.mark.asyncio
async def test_tool_loop_accepts_finish_answer_control_on_second_call() -> None:
    runtime = AgentRuntime(
        cast(object, None),
        Settings(llm_api_key=""),
        chat_model=FakeChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "catalog_search",
                            "args": {"query": "无线鼠标"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                _control_message(
                    "finish_answer",
                    response="根据查询结果，推荐这三款无线鼠标。",
                    used_tool_call_ids=["call-1"],
                ),
            ]
        ),
    )
    state = cast(
        AgentState,
        {
            "message": "推荐无线鼠标",
            "route_plan": _tool_route_plan("推荐无线鼠标"),
            "history": [],
            "tool_results": [],
            "tool_waves": [],
            "tool_wave_count": 0,
            "orchestrator_call_count": 0,
        },
    )

    first = await runtime._orchestrate(state)
    assert first["decision"]["type"] == "tool_calls"

    first["tool_wave_count"] = 1
    first["tool_results"] = [
        {
            "tool_call_id": "call-1",
            "name": "catalog_search",
            "execution": {
                "ok": True,
                "output": {
                    "result_type": "products",
                    "products": [{"sku_id": 1}],
                },
            },
        }
    ]
    second = await runtime._orchestrate(first)

    assert second["orchestrator_call_count"] == 2
    assert second["decision"]["type"] == "grounded_response"
    assert second["decision"]["response"] == "根据查询结果，推荐这三款无线鼠标。"
