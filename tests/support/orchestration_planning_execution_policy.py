from langchain_core.messages import AIMessage


def goal(query="查询键盘", disposition="tool_planning", identity="goal_1"):
    return {"id": identity, "query": query, "source_text": query, "disposition": disposition}


def task(tool="catalog_search", **arguments):
    return {
        "goal_id": "goal_1",
        "id": "task_1",
        "tool_name": tool,
        "arguments": {"query": "查询", **arguments},
    }


def route():
    from app.agent.legacy.routing import RequestRoutePlan, RoutedSubquery
    return RequestRoutePlan(
        rewritten_query="查询",
        subqueries=[
            RoutedSubquery(
                **goal("查询"),
                reason_code="test",
                planning_pending=True,
            )
        ],
    )


class SequenceModel:
    def __init__(self, name, outputs):
        self.name, self.outputs, self.calls = name, iter(outputs), []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        value = next(self.outputs)
        if isinstance(value, Exception):
            raise value
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.name,
                    "args": value,
                    "id": "call",
                }
            ],
        )
