from langchain_core.messages import AIMessage


def route(query, *, source_text=None, refs=()):
    from app.agent.legacy.routing import RequestRoutePlan, RoutedSubquery
    return RequestRoutePlan(
        rewritten_query=query,
        subqueries=[
            RoutedSubquery(
                id="goal_1",
                query=query,
                source_text=source_text or query,
                disposition="tool_planning",
                reason_code="test",
                planning_pending=True,
                context_refs=list(refs),
            )
        ],
    )


def execution(tool, arguments, **kwargs):
    from app.agent.legacy.goal_planning import (
    ExecutionPlan,
)
    return ExecutionPlan.model_validate(
        {
            "tasks": [
                {
                    "id": "task_1",
                    "goal_id": "goal_1",
                    "tool_name": tool,
                    "arguments": arguments,
                    **kwargs,
                }
            ]
        }
    )


class FixedModel:
    def __init__(self, name, args):
        self.name, self.args, self.messages = name, args, []

    async def ainvoke(self, messages):
        self.messages.append(messages)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": self.name,
                    "args": self.args,
                    "id": "call",
                }
            ],
        )
