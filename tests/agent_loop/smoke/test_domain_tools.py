"""Real SDK -> P1 adapter -> formal domain services -> program evidence, offline."""

import httpx
import pytest
from app.agent.strands_tools import StrandsToolBindings
from app.core.strands_model import TransportUsage, strands_model
from strands import Agent
from strands.hooks import AfterToolsEvent, BeforeToolsEvent
from strands.tools.executors import SequentialToolExecutor

from agent_loop.support.domain_tools import TOOL_ARGUMENTS, adapter, make_catalog
from agent_loop.support.strands_provider import SETTINGS, Provider, call


@pytest.mark.smoke
@pytest.mark.asyncio
@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
async def test_five_tools_through_real_sdk(monkeypatch, name):
    query = adapter(make_catalog(monkeypatch))
    bindings = StrandsToolBindings(query)
    operations = []

    def before(event: BeforeToolsEvent):
        for content in event.message["content"]:
            if "toolUse" in content:
                use = content["toolUse"]
                decoded = bindings.decode(use)
                operation_id = query.prepare(decoded.goal_id, use["name"], decoded.arguments)
                operations.append(operation_id)
                bindings.bind(use["toolUseId"], operation_id)

    def after(event: AfterToolsEvent):
        # P1 smoke stops after one execution, not a production completion protocol.
        event.end_turn = True

    provider = Provider([[call(name, {"goal_id": "g1", "arguments": TOOL_ARGUMENTS[name]})]])
    usage = TransportUsage()
    async with strands_model(SETTINGS, usage, transport=httpx.MockTransport(provider)) as model:
        agent = Agent(
            model=model, tools=bindings.tools(), hooks=[before, after], callback_handler=None,
            tool_executor=SequentialToolExecutor(), retry_strategy=None, context_manager=False,
        )
        await agent.invoke_async("P1 合成工具接入测试")
    assert usage.attempts == 1
    record, = query.attempts(operations[0])
    assert record.result.ok, record.result.error
    assert record.invocation.goal_id == "g1"
    assert record.invocation.tool_name == name
    assert record.result.output["result_type"] not in {"empty", "not_found", "planning_failed"}
    assert query.evidence.reference("g1", record.evidence_id).result == record.result
    assert "user_id" not in record.invocation.arguments
