"""Offline checks for isolated review context and unchanged shared-planner defaults."""

import json

import pytest
from app.schemas.catalog_search import CatalogSearchRequest, SearchPlan
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.structured_planning import generate_tool_plan
from langchain_core.messages import AIMessage
from pydantic import BaseModel


class Decision(BaseModel):
    value: str


@pytest.mark.asyncio
@pytest.mark.parametrize('review_context', [None, 'domain rules only'])
async def test_review_context_preserves_default_and_original_request(review_context):
    class Model:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, messages):
            self.calls.append(messages)
            return AIMessage(content=json.dumps(
                {'value': 'done'} if len(self.calls) == 1 else {'verdict': 'approve'}))

    model = Model()
    request = {'query': 'independent request', 'scope': {'spu_ids': [8]}}
    result = await generate_tool_plan(
        model, schema=Decision, required=('value',), prompt='generation task',
        payload=request, validate=lambda _: {}, review=True, review_context=review_context,
    )
    assert result.value == 'done'
    assert len(model.calls) == 2
    review = model.calls[1]
    assert review[0].content.startswith(review_context or 'generation task')
    if review_context:
        assert 'generation task' not in review[0].content
    assert json.loads(review[1].content)['original_request'] == request


@pytest.mark.asyncio
async def test_catalog_supplies_review_contract_without_generation_schema(monkeypatch):
    captured = {}

    async def generate(*args, **kwargs):
        captured.update(kwargs)
        return SearchPlan()

    monkeypatch.setattr('app.tools.catalog_search.planner.generate_tool_plan', generate)
    await CatalogSearchPlanner(object()).plan(CatalogSearchRequest(query='查一下商品信息'))
    assert '生成步骤与输出契约' in captured['prompt']
    assert '生成步骤与输出契约' not in captured['review_context']
    assert '商品计划复核职责' in captured['review_context']
    assert '本工具只负责商品发现，operation固定search。' in captured['review_context']
    assert captured['review'] is True
