from typing import Any

from langchain_core.messages import AIMessage


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
