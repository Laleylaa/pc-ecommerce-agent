"""Scripted workflow contracts, independent of natural-language quality."""
import json
from types import SimpleNamespace

import pytest
from app.core.llm_calls import ModelCallError
from app.schemas.catalog_search import CatalogSearchRequest
from app.tools.catalog.planner import CatalogSearchPlanner

from tests.support.tool_plan_fixtures import search_plan


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps(next(self.responses)))


def scope():
    return {
        "scope_kind": "catalog",
        "category": None,
        "search_text": None,
        "target": None,
        "needs_specifications": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition', ['unsupported', 'clarification_required', 'ready'])
async def test_stop_and_ready_plans_both_reach_review(disposition):
    plan = search_plan({'disposition': disposition})
    model = Script([scope(), plan, {'verdict': 'approve'}])
    result = await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query='固定脚本请求'))
    assert result.disposition == disposition
    assert len(model.calls) == 3
    review = json.loads(model.calls[2][1].content)
    assert review['candidate_plan']['disposition'] == disposition
    assert review['original_request']['query'] == '固定脚本请求'


@pytest.mark.asyncio
async def test_repeated_invalid_reviews_exhaust_shared_repair_budget():
    model = Script([scope(), search_plan(), *[{'verdict': f'invalid-{i}'} for i in range(3)]])
    with pytest.raises(ModelCallError):
        await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query='固定脚本请求'))
    assert len(model.calls) == 5
    assert all('candidate_plan' in json.loads(c[1].content) for c in model.calls[2:])
