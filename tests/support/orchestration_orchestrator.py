from app.tools.contracts import (
    LLM_SAFE_TOOL_NAMES,
)
from langchain_core.messages import AIMessage


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
