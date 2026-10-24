"""Production scope/feedback contracts; separate from tool-selection evaluation."""

import pytest
from app.agent.loop.query_schema import QueryGoal
from app.agent.loop.strands_tools import StrandsToolBindings
from app.agent.loop.tool_adapter import AdmissionError, QueryToolAdapter
from app.schemas.catalog_search import SearchScope
from app.tools.contracts import ToolRuntimeContext, static_tool_catalog
from jsonschema import Draft202012Validator
from pydantic import ValidationError

pytestmark = pytest.mark.contract

def bindings():
    return StrandsToolBindings(QueryToolAdapter(
        static_tool_catalog(), ToolRuntimeContext(user_id=41), admit_goal=lambda _: None,
    ))


@pytest.mark.parametrize('scope,valid', [
    ({'sku_ids': [9101]}, True), ({'spu_ids': [91]}, True),
    ({'sku_ids': [9101], 'spu_ids': []}, True),
    ({'sku_ids': [9101], 'spu_ids': [91]}, False),
])
def test_exported_scope_agrees_with_runtime(scope, valid):
    tool = next(t for t in bindings().tools() if t.tool_name == 'catalog_lookup')
    schema = tool.tool_spec['inputSchema']['json']
    call = {'goal_id': 'g', 'arguments': {'query': '当前价格', 'scope': scope}}
    assert Draft202012Validator(schema).is_valid(call) == valid
    if valid:
        SearchScope.model_validate(scope)
    else:
        with pytest.raises(ValidationError) as error:
            SearchScope.model_validate(scope)
        assert error.value.errors()[0]['type'] == 'scope_type_conflict'


def test_conflicting_scope_returns_actionable_feedback_without_echoing_query():
    with pytest.raises(AdmissionError) as error:
        bindings().decode({'name': 'catalog_lookup', 'toolUseId': 't', 'input': {
            'goal_id': 'g', 'arguments': {'query': 'private sentinel',
                                        'scope': {'sku_ids': [9101], 'spu_ids': [91]}},
        }})
    assert str(error.value) == 'invalid_input'
    assert 'sku_ids and spu_ids cannot both' in error.value.feedback
    assert 'do not remove' in error.value.feedback
    assert 'private sentinel' not in error.value.feedback


def test_reference_schema_explains_future_result_boundary():
    description = QueryGoal.model_json_schema()['properties']['references']['description']
    assert 'working_memory' in description and 'upstream goal' in description


@pytest.mark.asyncio
async def test_scope_rejection_reaches_model_without_executing_domain_tool():
    import json

    import httpx
    from agent_loop.support.strands_provider import SETTINGS, Provider, call
    from app.agent.loop.query_loop import QuerySession
    from app.core.loop_budget import LoopBudget
    from app.core.loop_models import loop_models
    from app.schemas.context import PreparedTurn

    message = '查 SKU 9101 价格'
    goal = {'id': 'g', 'source_text': message, 'query': message,
            'disposition': 'tool_planning'}
    provider = Provider([
        [call('register_goals', {'registration': {'goals': [goal]}}),
         call('catalog_lookup', {'goal_id': 'g', 'arguments': {
             'query': message, 'scope': {'sku_ids': [9101], 'spu_ids': [91]},
         }})],
        [call('register_goals', {'registration': {'goals': [goal]}}),
         call('query_complete', {'completion': {'reason': 'fixture stops after rejection'}})],
    ])
    budget = LoopBudget()
    session = QuerySession(PreparedTurn(
        user_id=41, conversation_id=1, user_message_id=1, run_id=1, message=message,
    ), static_tool_catalog(), budget)
    async with loop_models(SETTINGS, budget, transport=httpx.MockTransport(provider)) as models:
        await session.run(models[0])
    assert session.stop_reason == 'completed'
    assert session.adapter.evidence.all_records() == []
    assert session.rejections == ['invalid_input']
    feedback = json.dumps(provider.requests[1]['messages'])
    assert 'sku_ids and spu_ids cannot both' in feedback
    assert 'do not remove' in feedback
