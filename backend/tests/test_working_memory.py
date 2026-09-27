from typing import cast

from langchain_core.messages import HumanMessage

from app.agent.artifacts import initialize_task_runtime
from app.agent.graph import AgentRuntime, _orchestrator_messages
from app.agent.state import AgentState
from app.core.config import Settings


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


def test_orchestrator_messages_include_current_request_context() -> None:
    state = cast(
        AgentState,
        {
            "message": "Recommend a wireless mouse",
            "route_plan": _tool_route_plan("Recommend a wireless mouse"),
            "history": [],
            "tool_wave_count": 1,
            "tool_waves": [],
        },
    )

    messages = _orchestrator_messages(state, call_count=2)

    current_message = cast(HumanMessage, messages[-1]).content
    assert "Recommend a wireless mouse" in str(current_message)
    assert "completed_tool_waves" in str(current_message)
    assert "current_orchestrator_call" in str(current_message)


def test_orchestrator_messages_use_artifacts_without_raw_tool_observations() -> None:
    state = cast(
        AgentState,
        {
            "message": "Recommend a wireless mouse",
            "route_plan": _tool_route_plan("Recommend a wireless mouse"),
            "history": [],
            "tool_wave_count": 1,
            "task_status": {
                "sq_1": {
                    "task_id": "sq_1",
                    "goal_id": "sq_1",
                    "answer_role": "user_facing",
                    "status": "unavailable",
                    "reason": "tool_outcome:empty",
                }
            },
            "task_artifacts": {
                "sq_1": {
                    "task_id": "sq_1",
                    "goal_id": "sq_1",
                    "artifact_type": "products",
                    "usable": False,
                    "value": {"products": []},
                    "evidence": [],
                    "source_tool_call_id": "call-1",
                    "source_tool_name": "catalog_search",
                    "extractor": "deterministic",
                    "reason": "empty",
                }
            },
            "tool_waves": [
                {
                    "wave": 1,
                    "calls": [
                        {
                            "id": "call-1",
                            "name": "catalog_search",
                            "arguments": {"query": "wireless mouse"},
                        }
                    ],
                    "results": [
                        {
                            "tool_call_id": "call-1",
                            "name": "catalog_search",
                            "execution": {
                                "tool_name": "catalog_search",
                                "ok": True,
                                "output": {"result_type": "empty", "products": []},
                                "error": None,
                            },
                        }
                    ],
                }
            ],
        },
    )

    messages = _orchestrator_messages(state, call_count=2)

    assert not any(getattr(message, "tool_call_id", None) for message in messages)
    assert '"source_tool_call_id": "call-1"' in str(messages[-1].content)
    assert "<answer_context>" in str(messages[-1].content)
    assert '"rewritten_query": "Recommend a wireless mouse"' in str(messages[-1].content)
    assert '"semantic_outcome": "answered_no_match"' in str(messages[-1].content)
    assert '"result_type": "empty"' not in str(messages[-1].content)


def test_fallback_planner_routes_order_lookup_from_route_plan() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    query = "Where is order 202607020001?"
    state = cast(
        AgentState,
        {"message": query, "route_plan": _tool_route_plan(query), "tool_results": []},
    )
    initialize_task_runtime(state)

    decision = runtime._fallback_planner_decision(state)

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name == "order_lookup"
    assert decision.tool_calls[0].arguments["order_id"] == 202607020001


def test_fallback_planner_routes_policy_search_from_route_plan() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    query = "return policy"
    state = cast(
        AgentState,
        {"message": query, "route_plan": _tool_route_plan(query), "tool_results": []},
    )
    initialize_task_runtime(state)

    decision = runtime._fallback_planner_decision(state)

    assert decision.type == "tool_calls"
    assert decision.tool_calls[0].name in {"policy_search", "knowledge_search"}


def test_fallback_router_handles_identity_without_planner() -> None:
    runtime = AgentRuntime(cast(object, None), Settings(llm_api_key=""))
    state = cast(AgentState, {"message": "你能做什么？", "working_memory": {}})

    plan = runtime._fallback_route_plan(state)

    assert plan.subqueries[0].disposition == "direct_response"
