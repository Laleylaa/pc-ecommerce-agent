"""Classify model failures without network access or leaking provider request data."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from app.core.llm_calls import ModelCallError
from app.tools.structured_planning import generate_tool_plan
from openai import APITimeoutError
from pydantic import BaseModel


class Plan(BaseModel):
    value: int


class ReviewFailure:
    def __init__(self, failure):
        self.failure = failure
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(content='{"value":1}')
        raise self.failure


def wrapped_timeout(cause=None):
    error = APITimeoutError(request=httpx.Request("POST", "https://private.invalid/secret"))
    error.__cause__ = cause
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("error,source", [
    (httpx.ConnectTimeout("secret"), "connect"),
    (httpx.ReadTimeout("secret"), "read"),
    (httpx.WriteTimeout("secret"), "write"),
    (httpx.PoolTimeout("secret"), "pool"),
    (wrapped_timeout(httpx.ReadTimeout("secret")), "read"),
    (wrapped_timeout(), "provider_unspecified"),
    (TimeoutError("secret"), "unspecified"),
])
async def test_review_timeout_reports_phase_and_cause_without_resetting_retries(error, source):
    model = ReviewFailure(error)
    with pytest.raises(ModelCallError) as caught:
        await generate_tool_plan(
            model, schema=Plan, required=("value",), prompt="test",
            payload={"query": "查订单"}, review=True,
        )
    assert caught.value.code == "planning_call_timeout"
    details = caught.value.details
    assert details["stage"] == "tool_review" and details["timeout_source"] == source
    assert details["attempt"] == model.calls == 3  # One generation, two review attempts.
    assert 0 <= details["attempt_seconds"] <= details["elapsed_seconds"]
    assert 0 < details["remaining_seconds"] <= 60
    assert "secret" not in json.dumps(details) and "private.invalid" not in json.dumps(details)


@pytest.mark.asyncio
async def test_our_deadline_is_not_reported_as_api_connection_timeout():
    class SlowReview:
        calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(content='{"value":1}')
            await asyncio.Event().wait()

    model = SlowReview()
    with pytest.raises(ModelCallError) as caught:
        await generate_tool_plan(
            model, schema=Plan, required=("value",), prompt="test",
            payload={"query": "查订单"}, review=True, timeout_seconds=0.02,
        )
    assert caught.value.code == "planning_call_timeout"
    assert caught.value.details["stage"] == "tool_review"
    assert caught.value.details["timeout_source"] == "local_deadline"
    assert caught.value.details["remaining_seconds"] == 0
    assert model.calls == 2  # Exhausted shared budget cannot start another attempt.
