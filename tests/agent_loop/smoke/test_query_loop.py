"""A complete SDK query/observation/controlled-stop smoke check."""

import httpx
import pytest
from agent_loop.support.strands_provider import (
    FINISH,
    FIRST,
    SECOND,
    SETTINGS,
    Provider,
)

from evals.agent.probe_agent_loop import run_probe


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_multistep_observation_and_control_exit_no_extra_model_call():
    provider = Provider([[FIRST], [SECOND], [FINISH]])
    result = await run_probe(SETTINGS, transport=httpx.MockTransport(provider))
    assert result["passed"] and result["stop_reason"] == "completed"
    assert result["http_attempts"] == result["model_attempts"] == 3
    assert result["known_input_tokens"] == 33 and result["known_output_tokens"] == 21
    assert result["usage_missing_attempts"] == 0
    assert result["calls"] == ["first", "second"]
    assert '"next_key": "second"' in provider.requests[1]["messages"][-1]["content"]
    assert "e2" in provider.requests[2]["messages"][-1]["content"]
