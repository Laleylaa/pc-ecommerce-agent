"""SDK contracts, failure budgets, timeout and cancellation behavior."""

import asyncio

import httpx
import pytest
from app.core.strands_model import TransportUsage, strands_model

from agent_loop.smoke.probe_agent_loop import ProbeState, build_probe, run_probe
from agent_loop.support.strands_provider import (
    FINISH,
    FIRST,
    SECOND,
    SETTINGS,
    Provider,
    call,
)

pytestmark = pytest.mark.contract


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    call("query_complete", "{broken"),
    call("query_complete", {"completion": {"goal_id": "probe", "evidence_ids": ["e1", "fake"]}}),
    call("query_complete", {"completion": {
        "goal_id": "probe", "evidence_ids": ["e1", "e2"], "user_id": 2,
    }}),
])
async def test_invalid_completion_can_repair_once(invalid):
    provider = Provider([[FIRST], [SECOND], [invalid], [FINISH]])
    result = await run_probe(SETTINGS, transport=httpx.MockTransport(provider))
    assert result["passed"] and result["http_attempts"] == 4


@pytest.mark.asyncio
async def test_repeated_bad_completion_stops_with_failure():
    provider = Provider([[FINISH], [FINISH]])
    result = await run_probe(SETTINGS, transport=httpx.MockTransport(provider))
    assert not result["passed"] and result["stop_reason"] == "invalid_completion"
    assert result["http_attempts"] == 2 and result["calls"] == []


@pytest.mark.asyncio
async def test_finish_mixed_with_lookup_rejects_entire_batch():
    provider = Provider([[FIRST, FINISH], [FIRST], [SECOND], [FINISH]])
    result = await run_probe(SETTINGS, transport=httpx.MockTransport(provider))
    assert result["passed"] and result["calls"] == ["first", "second"]
    assert "Submit query_complete alone" in provider.requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_free_text_is_not_success_and_missing_usage_is_unknown():
    provider = Provider([[]], usage=False)
    result = await run_probe(SETTINGS, transport=httpx.MockTransport(provider))
    assert not result["passed"] and result["stop_reason"] == "missing_completion"
    assert result["usage_missing_attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 503])
async def test_no_sdk_or_client_hidden_retries(status):
    requests = []

    def fail(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "synthetic"}})

    result = await run_probe(SETTINGS, transport=httpx.MockTransport(fail))
    assert not result["passed"] and result["stop_reason"] == "model_error"
    assert len(requests) == result["http_attempts"] == 1
    assert result["usage_missing_attempts"] == 1


@pytest.mark.asyncio
async def test_repeated_tool_loop_stops_at_model_budget():
    result = await run_probe(SETTINGS, transport=httpx.MockTransport(Provider([[FIRST]] * 8)))
    assert not result["passed"] and result["stop_reason"] == "model_budget"
    assert result["http_attempts"] == 8


@pytest.mark.asyncio
async def test_timeout_cancels_pending_transport():
    cleaned = asyncio.Event()

    async def hang(request):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    result = await run_probe(SETTINGS, transport=httpx.MockTransport(hang), timeout=0.02)
    assert result["stop_reason"] == "timeout" and cleaned.is_set()
    assert result["http_attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("during_tool", [False, True])
async def test_external_cancel_propagates_and_releases_work(during_tool):
    entered, cleaned = asyncio.Event(), asyncio.Event()
    state, usage = ProbeState(), TransportUsage()

    async def hang(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    transport = httpx.MockTransport(Provider([[FIRST]]) if during_tool else hang)
    async with strands_model(SETTINGS, usage, transport=transport) as model:
        client = model._custom_client
        agent = build_probe(model, state, hang=hang if during_tool else None)
        task = asyncio.create_task(agent.invoke_async("probe"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set() and usage.attempts == 1 and state.completion is None
    assert client.is_closed()
