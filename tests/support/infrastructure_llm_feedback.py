from typing import Literal

from app.tools.contracts import DefaultToolContractProvider
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(strict=True, ge=1, le=5)
    kind: Literal["one", "many"]


class Envelope(BaseModel):
    items: list[Item]


class SequenceModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return output


def execution_data():
    return {"tasks": [{"goal_id": "goal_1", "id": "task_1", "tool_name": "catalog_search",
                       "arguments": {"query": "查询键盘", "limit": 2}}]}


def execution_call(data, name="submit_execution_plan"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": data, "id": "plan"}])


def execution_validator(plan):
    from app.agent.legacy.goal_planning import (
    validate_execution,
)
    from app.agent.legacy.routing import RequestRoutePlan, RoutedSubquery
    route = RequestRoutePlan(rewritten_query="查询键盘", subqueries=[
        RoutedSubquery(id="goal_1", query="查询键盘", disposition="tool_planning",
                       reason_code="test", planning_pending=True),
    ])
    validate_execution(plan, route, DefaultToolContractProvider(), {})
