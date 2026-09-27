from typing import cast

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph

from app.agent.artifacts import initialize_task_runtime
from app.agent.decisions import decision_from_ai_message
from app.agent.graph import (
    MAX_ORCHESTRATOR_CALLS,
    MAX_TOOL_WAVES,
    AgentRuntime,
    _fallback_answer,
    _tag_from_decision,
)
from app.agent.prompts import (
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
    TOOL_RESULT_INTERPRETATION_POLICY,
    TOOL_SELECTION_RULES,
    build_orchestrator_system_prompt,
    build_orchestrator_user_prompt,
    build_tool_failure_prompt,
)
from app.agent.state import AgentState
from app.core.config import Settings
from app.tools.contracts import (
    LLM_SAFE_TOOL_NAMES,
    BoundTool,
    DefaultToolContractProvider,
    RegistryToolExecutor,
    ToolCatalog,
)
from app.tools.schemas import OrderLookupOutput


class FakeChatModel:
    def __init__(self, responses: list[AIMessage]):
        self.responses = responses
        self.call_count = 0
        self.bound_tool_sets: list[set[str]] = []

    def bind_tools(self, tools: list[dict], **_: object):
        tools_by_name = {tool["function"]["name"]: tool for tool in tools}
        self.bound_tool_sets.append(set(tools_by_name))
        for name in set(LLM_SAFE_TOOL_NAMES) & set(tools_by_name):
            parameters = tools_by_name[name]["function"]["parameters"]
            assert "subquery" in parameters["properties"]
            assert "subquery" in parameters["required"]
            assert "query" not in parameters["properties"]
            assert "query" not in parameters["required"]
        return self

    async def ainvoke(self, messages):
        assert messages
        response = self.responses[self.call_count]
        self.call_count += 1
        return response


class RecordingBoundModel:
    def __init__(self, tool_names: set[str], tool_choice: str | None):
        self.tool_names = tool_names
        self.tool_choice = tool_choice


class RecordingBindableModel:
    def __init__(self):
        self.bindings: list[RecordingBoundModel] = []

    def bind_tools(
        self,
        tools: list[dict],
        **kwargs: object,
    ) -> RecordingBoundModel:
        binding = RecordingBoundModel(
            {tool["function"]["name"] for tool in tools},
            str(kwargs["tool_choice"]) if kwargs.get("tool_choice") else None,
        )
        self.bindings.append(binding)
        return binding


def _control_message(name: str, **arguments) -> AIMessage:
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


def _tool_route_plan(query: str) -> dict:
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


def test_orchestrator_prompt_separates_fact_sources_without_repeating_schemas() -> None:
    assert "catalog_search、catalog_compare、catalog_facets、order_lookup" in (
        ORCHESTRATOR_OBSERVATION_PROMPT
    )
    assert "policy_search、knowledge_search" in ORCHESTRATOR_OBSERVATION_PROMPT
    assert "文档不能覆盖" in ORCHESTRATOR_OBSERVATION_PROMPT
    assert "parameters" not in ORCHESTRATOR_SYSTEM_PROMPT


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


def test_orchestrator_prompt_preserves_sku_and_spu_sales_semantics() -> None:
    assert "sku_sales_count 是当前版本销量" in ORCHESTRATOR_OBSERVATION_PROMPT
    assert "sales_count 是整个商品系列累计销量" in ORCHESTRATOR_OBSERVATION_PROMPT
    assert "不得混用" in ORCHESTRATOR_OBSERVATION_PROMPT
    rules = "\n".join(TOOL_SELECTION_RULES)
    assert "sku_sales_count 表示当前 SKU" in rules
    assert "不得用当前累计销量推断趋势" in ORCHESTRATOR_PLANNING_PROMPT


def test_orchestrator_prompt_requires_native_control_action_without_type_protocol() -> None:
    assert "只返回一个或多个原生业务 Tool Call" in ORCHESTRATOR_PLANNING_PROMPT
    assert "只调用一个已绑定控制动作" in ORCHESTRATOR_OBSERVATION_PROMPT
    assert "第一行必须且只能输出 TYPE" not in ORCHESTRATOR_OBSERVATION_PROMPT


def test_tool_result_prompt_explains_scenario_semantics_without_copying_rules() -> None:
    policy = TOOL_RESULT_INTERPRETATION_POLICY

    assert "只有 `ok=false` 才是调用错误" in policy
    assert "usage_mapping.status=applied" in policy
    assert "`expanded`" in policy
    assert "usage_mapping_unavailable" in policy
    assert "usage_mapping.required" in policy
    assert "`preferred` 只影响排序" in policy
    assert "厂商认证" in policy
    assert "静音红轴" not in policy
    assert "weight_g" not in policy
    assert policy in ORCHESTRATOR_OBSERVATION_PROMPT
    assert policy not in ORCHESTRATOR_PLANNING_PROMPT


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


def test_orchestrator_prompt_loads_only_the_current_phase_policy() -> None:
    planning_prompt = build_orchestrator_system_prompt()
    observation_prompt = build_orchestrator_system_prompt(
        tool_results=[
            {
                "tool_call_id": "call-1",
                "name": "catalog_search",
                "execution": {
                    "ok": True,
                    "output": {"result_type": "empty", "products": []},
                },
            }
        ]
    )

    assert planning_prompt == ORCHESTRATOR_PLANNING_PROMPT
    assert "<tool_routing>" in planning_prompt
    assert "<tool_input_protocol>" in planning_prompt
    assert "<customer_voice>" not in planning_prompt
    assert "<tool_result_interpretation>" not in planning_prompt
    assert "<business_result_response_policy>" not in planning_prompt

    assert observation_prompt == ORCHESTRATOR_OBSERVATION_PROMPT
    assert "<tool_result_interpretation>" in observation_prompt
    assert "<business_result_response_policy>" in observation_prompt
    assert "<customer_voice>" in observation_prompt
    assert "<tool_routing>" not in observation_prompt
    assert "<routing_examples>" not in observation_prompt
    assert TOOL_INPUT_PROTOCOL not in observation_prompt
    assert TOOL_RECOVERY_PROTOCOL not in observation_prompt
    assert len(planning_prompt) < 4_000
    assert len(observation_prompt) < 7_000


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


def test_fallback_planner_routes_catalog_facets_questions() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "message": "你们有哪些鼠标品牌？",
            "route_plan": _tool_route_plan("你们有哪些鼠标品牌？"),
            "tool_results": [],
            "tool_waves": [],
            "tool_wave_count": 0,
        },
    )
    initialize_task_runtime(state)
    decision = runtime._fallback_planner_decision(state)

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name == "catalog_facets"
    assert decision.tool_calls[0].arguments == {
        "query": "你们有哪些鼠标品牌？",
        "limit": 20,
    }


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

    assert "Logitech（12 条 SKU 记录）" in answer
    assert "Razer（8 条 SKU 记录）" in answer


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


def test_third_orchestrator_call_cannot_start_third_tool_wave() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(
        AgentState,
        {
            "tool_wave_count": MAX_TOOL_WAVES,
        },
    )
    decision = decision_from_ai_message(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-3",
                    "name": "catalog_search",
                    "args": {"query": "鼠标"},
                    "type": "tool_call",
                }
            ],
        )
    )

    guarded = runtime._validate_decision_budget(
        state,
        decision,
        MAX_ORCHESTRATOR_CALLS,
    )

    assert guarded.type == "unavailable_response"
    assert guarded.tool_calls == []
    assert guarded.control_action == "finish_unavailable"


@pytest.mark.asyncio
async def test_order_user_id_is_injected_by_runtime() -> None:
    captured_input: dict | None = None
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async def handler(request) -> OrderLookupOutput:
        nonlocal captured_input
        captured_input = request.model_dump(mode="json", exclude_none=True)
        return OrderLookupOutput(result_type="not_found")

    executor = RegistryToolExecutor(
        cast(object, None),
        Settings(llm_api_key=""),
        catalog=ToolCatalog([BoundTool(contract, handler)]),
    )

    result = await executor.execute(contract, {"order_id": 42}, {"user_id": 7})

    assert result.ok is True
    assert captured_input == {"user_id": 7, "order_id": 42, "limit": 5, "offset": 0}
