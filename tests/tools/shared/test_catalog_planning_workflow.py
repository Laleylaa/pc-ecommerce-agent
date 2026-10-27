"""Scripted workflow contracts, independent of natural-language quality."""
import json
from types import SimpleNamespace

import pytest
from app.core.llm_calls import ModelCallError
from app.schemas.catalog_search import CatalogSearchRequest
from app.tools.catalog.facets_execution import execute_facets
from app.tools.catalog.facets_planner import CatalogFacetPlanner
from app.tools.catalog.planner import CatalogSearchPlanner
from app.tools.schemas import CatalogFacetInput

from tests.support.tool_plan_fixtures import facet_plan, search_plan


class Script:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps(next(self.responses)))


@pytest.mark.asyncio
async def test_quantity_is_bound_before_review_even_if_internal_model_changes_it():
    model = Script([scope(), search_plan({'requested_limit': 9}), {'verdict': 'approve'}])
    result = await CatalogSearchPlanner(model).plan(CatalogSearchRequest(
        query='推荐一款键盘', requested_count=1, limit=10,
    ))
    assert result.requested_limit == 1
    review = json.loads(model.calls[2][1].content)
    assert review['candidate_plan']['requested_limit'] == 1
    assert review['original_request']['effective_requested_count'] == 1
    assert review['original_request']['limit'] == 10


@pytest.mark.asyncio
async def test_facets_multiple_dimensions_return_split_without_querying():
    plan = facet_plan({'supported': False, 'split_required': True,
                       'unsupported_reason': '品牌与轴体需要分别统计'})
    model = Script([scope(), plan, {'verdict': 'approve'}])
    request = CatalogFacetInput(query='有哪些品牌和轴体')
    result = await CatalogFacetPlanner(model).plan_facets(request)

    class NoRead:
        async def load(self, *args, **kwargs):
            pytest.fail('multi-dimensional plan must not query a partial facet')

    output = await execute_facets(NoRead(), request, result)
    assert output.result_type == 'empty'
    assert output.diagnostics[0].code == 'multiple_targets'
    assert output.diagnostics[0].recommended_action == 'split_tasks'


def scope():
    return {
        "scope_kind": "catalog",
        "category": None,
        "identity_text": None,
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


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,scope_kind,unit,view', [
    ('lookup', 'catalog', 'spu', 'list'),
    ('search', 'spu', 'sku', 'summary'),
])
async def test_scope_label_does_not_override_detailed_tool_purpose(
    operation, scope_kind, unit, view,
):
    """CT-001/002: scripted plans test routing structure, not model interpretation."""
    query = '版本事实列表' if operation == 'lookup' else '在指定范围选两个版本'
    plan = search_plan({'operation': operation, 'candidate_unit': unit, 'version_view': view})
    model = Script([scope() | {'scope_kind': scope_kind}, plan, {'verdict': 'approve'}])
    request = CatalogSearchRequest(query=query, scope={'spu_ids': [33]}, limit=2)
    result = await CatalogSearchPlanner(model, operation=operation).plan(request)
    assert (result.operation, result.candidate_unit, result.version_view) == (operation, unit, view)
    assert len(model.calls) == 3
    review = json.loads(model.calls[2][1].content)
    assert review['original_request']['scope'] == {'sku_ids': [], 'spu_ids': [33]}
    assert review['original_request']['limit'] == 2
    assert review['candidate_plan']['disposition'] == 'ready'


@pytest.mark.asyncio
@pytest.mark.parametrize('execution_fields', [{}, {'facet': 'spec_value'}])
async def test_facets_unsupported_plan_reaches_review_with_scope(execution_fields):
    """CT-004: unsupported is still reviewed; caller scope need not be in the plan."""
    plan = facet_plan({'supported': False, 'unsupported_reason': '对象价格事实应使用lookup'})
    for field in ('facet', 'count_scope', 'spec_key'):
        plan.pop(field)
    plan.update(execution_fields)
    model = Script([scope(), plan, {'verdict': 'approve'}])
    request = CatalogFacetInput(query='查询给定型号版本价格', scope={'spu_ids': [33]})
    result = await CatalogFacetPlanner(model).plan_facets(request)
    assert result.supported is False
    assert len(model.calls) == 3
    review = json.loads(model.calls[2][1].content)
    assert review['candidate_plan']['supported'] is False
    assert review['original_request']['scope'] == {'sku_ids': [], 'spu_ids': [33]}
    assert 'scope' not in review['candidate_plan']
    # Any repository access fails: reviewed stop plans must not run statistics.
    output = await execute_facets(object(), request, result)
    assert output.result_type == 'empty'
    assert output.diagnostics[0].code == 'request_not_supported'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,disposition', [
    ('search', 'unsupported'), ('lookup', 'clarification_required'),
    ('lookup', 'split_required'),
])
async def test_stop_plan_can_omit_execution_choices(operation, disposition):
    plan = {
        'operation': operation, 'disposition': disposition,
        'conditions': [], 'unresolved_requirements': [], 'reason': '需停止并说明限制',
    }
    model = Script([scope(), plan, {'verdict': 'approve'}])
    result = await CatalogSearchPlanner(model, operation=operation).plan(
        CatalogSearchRequest(query='固定脚本请求'),
    )
    assert result.disposition == disposition
    assert len(model.calls) == 3
