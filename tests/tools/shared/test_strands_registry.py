"""SDK registration/call smoke with synthetic domain output only."""
from unittest.mock import Mock

import pytest
from app.agent.loop.query_loop import QuerySession
from app.agent.loop.strands_tools import BusinessAgentTool, RegisteredBusinessTools
from app.agent.loop.tool_adapter import AdmissionError
from app.core.loop_budget import LoopBudget
from app.schemas.context import PreparedTurn
from app.tools.contracts import BoundTool, ToolCatalog, default_tool_contracts
from app.tools.schemas import OrderLookupOutput
from strands.models import Model
from strands.tools.registry import ToolRegistry

from tests.support.registry_tools import make_catalog


def session(catalog=None, user=41):
    return QuerySession(PreparedTurn(user_id=user, conversation_id=1, user_message_id=1,
                                    run_id=1, message="查订单"),
                        catalog or make_catalog(), LoopBudget())


async def invoke(agent, name, arguments, use_id="control"):
    use = {"name": name, "input": arguments, "toolUseId": use_id}
    events = [e async for e in agent.tool_registry.registry[name].stream(use, {})]
    return events[-1]["tool_result"]


@pytest.mark.asyncio
async def test_actual_registry_owns_all_bindings_and_specs():
    query = session()
    agent = query.build(Mock(spec=Model))
    registry = agent.tool_registry
    assert isinstance(registry, ToolRegistry)
    assert isinstance(query.adapter.catalog, RegisteredBusinessTools)
    assert query.adapter.catalog.registry is registry
    expected = {c.name for c in default_tool_contracts()} | {"register_goals", "query_complete"}
    assert set(registry.registry) == expected
    specs = registry.get_all_tool_specs()
    assert {s["name"] for s in query.visible_tools(specs)} == expected
    for definition in default_tool_contracts():
        tool = registry.registry[definition.name]
        assert isinstance(tool, BusinessAgentTool)
        assert tool.tool_spec["description"] == definition.model_description
        assert query.adapter.catalog.get_by_llm_name(definition.name) is tool.bound
        with pytest.raises(ValueError, match="already exists"):
            registry.register_tool(tool)
    for name in ("register_goals", "query_complete", "unknown"):
        assert query.adapter.catalog.get_by_llm_name(name) is None
    del registry.registry["order_lookup"]
    assert query.adapter.catalog.get_by_llm_name("order_lookup") is None


@pytest.mark.asyncio
async def test_sdk_controls_domain_identity_and_session_isolation():
    seen = []

    async def handler(request):
        seen.append(request.user_id)
        return OrderLookupOutput(result_type="not_found")

    contract = next(c for c in default_tool_contracts() if c.name == "order_lookup")
    definitions = ToolCatalog([BoundTool(contract, handler)])
    first, second = session(definitions, 41), session(definitions, 42)
    for query in (first, second):
        agent = query.build(Mock(spec=Model))
        result = await invoke(agent, "register_goals", {"registration": {"goals": [{
            "id": "g1", "source_text": "查订单", "query": "查订单", "disposition": "tool_planning",
        }]}})
        assert result["status"] == "success"
        operation = query.adapter.prepare("g1", "order_lookup", {"order_id": 1})
        query.bindings.bind("sdk", operation)
        result = await invoke(agent, "order_lookup", {
            "goal_id": "g1", "arguments": {"order_id": 1}}, "sdk")
        assert result["status"] == "success"
        assert len(query.adapter.evidence.all_records()) == 1
        assert (await invoke(agent, "order_lookup", {
            "goal_id": "g1", "arguments": {"order_id": 1}}, "sdk"))["status"] == "error"
        for name in ("register_goals", "query_complete", "unknown"):
            with pytest.raises(AdmissionError, match="unknown_or_non_query_tool"):
                query.adapter.prepare("g1", name, {})
        result = await invoke(agent, "query_complete", {"completion": {"reason": "done"}})
        assert result["status"] == "success"
        assert query.book.completion.reason == "done"
        assert len(query.adapter.evidence.all_records()) == 1
        with pytest.raises(AdmissionError, match="query_already_complete"):
            query.adapter.prepare("g1", "order_lookup", {"order_id": 2})
        assert {s["name"] for s in query.visible_tools(
            agent.tool_registry.get_all_tool_specs())} == {"register_goals", "query_complete"}
    assert seen == [41, 42]
    assert first.book is not second.book
    assert first.adapter.catalog.registry is not second.adapter.catalog.registry


@pytest.mark.asyncio
async def test_registered_schema_references_resolve_and_control_schemas_match():
    from app.agent.loop.query_schema import CompletionCall, RegistrationCall

    agent = session().build(Mock(spec=Model))
    def check_refs(value, root):
        if isinstance(value, dict):
            if "$ref" in value:
                target = root
                assert value["$ref"].startswith("#/")
                for segment in value["$ref"][2:].split("/"):
                    target = target[segment.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                check_refs(child, root)
        elif isinstance(value, list):
            for child in value:
                check_refs(child, root)

    for tool in agent.tool_registry.registry.values():
        schema = tool.tool_spec["inputSchema"]["json"]
        check_refs(schema, schema)
    for name, model in (("register_goals", RegistrationCall), ("query_complete", CompletionCall)):
        assert agent.tool_registry.registry[name].tool_spec["inputSchema"]["json"] == (
            model.model_json_schema())


def test_missing_contract_rejected_before_sdk_assembly():
    with pytest.raises(ValueError, match="missing tool contract"):
        ToolCatalog([BoundTool(None, lambda request: request)])
