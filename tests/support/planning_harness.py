"""Scripted production planning boundary; no tools, answer model, or persistence."""

from copy import deepcopy
from typing import Any

from app.agent.legacy.runtime import AgentRuntime
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.schemas.context import ContextMessage, PreparedTurn, WorkingMemoryV2
from langchain_core.messages import AIMessage


def goal(query: str, identity: str = "goal_1", **fields: Any) -> dict:
    return {
        "id": identity,
        "query": query,
        "source_text": query,
        "disposition": "tool_planning",
        **fields,
    }


def task(query: str, identity: str = "task_1", **fields: Any) -> dict:
    return {
        "id": identity,
        "goal_id": "goal_1",
        "tool_name": "catalog_search",
        "arguments": {"query": query},
        **fields,
    }


def output(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "plan"}])


class ScriptedPlanner:
    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.messages: list = []
        self.bound_tools: list = []

    def bind_tools(self, tools: list, **_: Any):
        self.bound_tools.append(tools)
        return self

    async def ainvoke(self, messages: list):
        self.messages.append(deepcopy(messages))
        assert self.outputs, "unexpected model call (including answer generation)"
        result = self.outputs.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class Context:
    def __init__(self, history: list, memory: dict):
        self.history, self.memory = history, memory

    async def prepare_turn(self, user_id: int, conversation_id: int | None, message: str):
        return PreparedTurn(
            user_id=user_id,
            conversation_id=conversation_id or 41,
            user_message_id=51,
            run_id=61,
            message=message,
            history=[ContextMessage.model_validate(item) for item in self.history],
            working_memory=WorkingMemoryV2.model_validate(self.memory),
        )

    async def complete_turn(self, *args, **kwargs):
        raise AssertionError("planning tests must stop before persistence")


class NoTools:
    async def execute(self, *args, **kwargs):
        raise AssertionError("planning tests must stop before business execution")


async def plan_request(
    message: str,
    goals: list[dict],
    tasks: list[dict],
    *,
    history: list | None = None,
    memory: dict | None = None,
    router_outputs: list | None = None,
    planner_outputs: list | None = None,
    blocked_goals: list | None = None,
    rewritten_query: str | None = None,
    policy=None,
    conversation_id=41,
):
    router = ScriptedPlanner(
        router_outputs
        if router_outputs is not None
        else [output("route_request", rewritten_query=rewritten_query or message, goals=goals)]
    )
    planner = ScriptedPlanner(
        planner_outputs
        if planner_outputs is not None
        else [output("submit_execution_plan", tasks=tasks, blocked_goals=blocked_goals or [])]
    )
    runtime = AgentRuntime(
        None,
        Settings(llm_api_key=""),
        router_model=router,
        chat_model=planner,
        context_service=Context(history or [], memory or {}),
        tool_executor=NoTools(),
        **({"planning_call_policy": policy} if policy is not None else {}),
    )
    state = runtime._initial_state(
        ChatRequest(message=message, conversation_id=conversation_id), user_id=7
    )
    frames = {}
    stream = runtime._build_graph().astream(state, stream_mode="updates")
    try:
        async for update in stream:
            for node, values in update.items():
                state.update(values)
                frames[node] = deepcopy(values)
                if node == "plan_tools" or (
                    node == "request_router" and runtime._dispatch_route(state) != "plan"
                ):
                    assert next(iter(frames)) == "input_rules"
                    assert set(frames) <= {
                        "input_rules",
                        "load_context",
                        "request_router",
                        "plan_tools",
                    }
                    assert state["tool_wave_count"] == 0 and state["tool_results"] == []
                    return runtime, state, router, planner, frames
    finally:
        await stream.aclose()
    raise AssertionError(f"planning boundary not reached: {list(frames)}")
