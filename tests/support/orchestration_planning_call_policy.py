from dataclasses import replace

import httpx
from openai import APIStatusError
from pydantic import BaseModel, Field


class Output(BaseModel):
    value: int = Field(gt=0)


class SequenceModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        assert self.outputs, "Unexpected extra model call"
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return await output() if callable(output) else output


def http_error(status, headers=None):
    response = httpx.Response(
        status, headers=headers, request=httpx.Request("POST", "https://model.invalid")
    )
    return APIStatusError("private provider response", response=response, body=None)


def policy(**changes):
    from app.agent.legacy.limits import PLANNING_CALL_POLICY
    return replace(PLANNING_CALL_POLICY, retry_delay_seconds=0, **changes)


async def generate(model, call_policy=None, **kwargs):
    from app.agent.legacy.goal_planning import (
    generate_plan,
)
    calls = []
    result = await generate_plan(
        model,
        "plan",
        Output,
        "test",
        {},
        lambda _: None,
        lambda: calls.append(True),
        policy=call_policy or policy(),
        **kwargs,
    )
    assert len(calls) == len(model.calls)
    return result
