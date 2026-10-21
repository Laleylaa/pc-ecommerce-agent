import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.core.llm_calls import invoke_before
from app.core.model_usage import collect_model_calls, model_call_counts


@pytest.mark.asyncio
async def test_model_counts_include_failed_attempts_and_isolate_concurrent_requests():
    async def request(stage, attempts):
        with collect_model_calls():
            model = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("failed")))

            async def child():
                with pytest.raises(RuntimeError):
                    await invoke_before(
                        model, [], asyncio.get_running_loop().time() + 5, stage=stage,
                    )

            await asyncio.gather(*(child() for _ in range(attempts)))
            return model_call_counts()

    assert await asyncio.gather(request("answer", 2), request("tool_planning", 3)) == [
        {"answer": 2}, {"tool_planning": 3},
    ]
    assert model_call_counts() is None
    with collect_model_calls():
        with pytest.raises(TimeoutError):
            await invoke_before(SimpleNamespace(), [], 0, stage="answer")
        assert model_call_counts() == {}  # Expired budget never reached the model.


@pytest.mark.asyncio
async def test_tool_internal_repair_counts_each_model_attempt():
    from app.tools.structured_planning import generate_tool_plan
    from langchain_core.messages import AIMessage
    from pydantic import BaseModel

    class Plan(BaseModel):
        value: int

    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
        AIMessage(content="not json"), AIMessage(content='{"value": 7}'),
    ]))
    with collect_model_calls():
        plan = await generate_tool_plan(
            model=model, schema=Plan, required=("value",), prompt="plan", payload={},
        )
        assert plan.value == 7
        assert model_call_counts() == {"tool_planning": 2}
