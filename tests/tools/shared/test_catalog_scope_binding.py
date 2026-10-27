"""Scope/detail ownership regressions through real planning and execution, with IO doubles."""

import json
from types import SimpleNamespace

import pytest
from app.core.llm_calls import ModelCallError
from app.repositories.catalog_search import CatalogIdentityFields, CatalogIdentityRecord
from app.schemas.catalog_search import CatalogSearchRequest
from app.tools.catalog.engine import CatalogSearchEngine
from app.tools.catalog.facets_planner import CatalogFacetPlanner
from app.tools.catalog.planner import CatalogSearchPlanner
from app.tools.catalog.service import CatalogSearchService
from app.tools.schemas import CatalogFacetInput

from tests.support.tool_plan_fixtures import facet_plan, search_plan
from tests.support.tool_planning import Model
from tests.tools.shared.catalog_business_support import Rows, row


def scope(**fields):
    return {
        'scope_kind': 'category', 'category': '键盘', 'identity_text': None,
        'target': None, 'needs_specifications': True,
    } | fields


class Sequence:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return SimpleNamespace(content=json.dumps(next(self.responses)))


class IdentityRows(Rows):
    async def load_identity(self, category, scope):
        rows = await self.load(category, scope)
        return [CatalogIdentityRecord(CatalogIdentityFields(
            r.product.sku_id, r.product.spu_id, r.product.title, r.product.spu_title,
            r.product.brand, r.product.category,
        ), r.active) for r in rows]


def keyboard(sku, spu, *, mechanical=True, **fields):
    record = row(sku, spu, **fields)
    name = f'测试{spu} ' + ('机械键盘' if mechanical else '薄膜键盘')
    record.product.spu_title = name
    record.product.title = f'{name} 版本{sku}'
    return record


def wireless_plan(**fields):
    return search_plan({
        'search_text': '机械键盘', 'conditions': [
            {'key': 'connection_type', 'values': ['无线'], 'evidence': '无线'},
            {'key': 'price', 'operator': 'lte', 'values': ['500'], 'evidence': '500元以内'},
        ], **fields,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize('candidate_scope, expected', [(None, {1, 2}), ([10], {1})])
async def test_discovery_text_survives_scope_and_executes_all_conditions(candidate_scope, expected):
    repository = IdentityRows([
        keyboard(1, 10, price=499, connection_type='蓝牙'),
        keyboard(2, 20, price=500, connection_type='三模'),
        keyboard(3, 30, price=501, connection_type='蓝牙'),
        keyboard(4, 40, price=100, mechanical=False, connection_type='蓝牙'),
        keyboard(5, 50, price=100, connection_type='有线'),
        # Price and wireless must hold on one SKU, not two versions of a model.
        keyboard(6, 60, price=100, connection_type='有线'),
        keyboard(7, 60, price=600, connection_type='蓝牙'),
    ])
    model = Model(
        scope(scope_kind='candidates' if candidate_scope else 'category'), wireless_plan(),
    )
    request = CatalogSearchRequest(
        query='推荐几款 500 元以内的无线机械键盘',
        **({'scope': {'spu_ids': candidate_scope}} if candidate_scope else {}),
    )
    result = await CatalogSearchService(repository, CatalogSearchPlanner(model)).search(request)
    assert {p.sku_id for p in result.products} == expected
    assert result.match_kind == 'exact'
    assert result.matched_sku_count == len(expected)
    reviewed = json.loads(model.review_calls[0][1].content)
    assert reviewed['candidate_plan']['search_text'] == '机械键盘'
    assert reviewed['candidate_plan']['conditions'] == wireless_plan()['conditions']
    assert reviewed['original_request']['query'] == request.query


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['catalog', 'category', 'candidates'])
async def test_plain_discovery_does_not_invent_search_text(kind):
    model = Model(scope(scope_kind=kind), search_plan())
    plan = await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query='推荐键盘'))
    assert plan.search_text is None


@pytest.mark.asyncio
async def test_review_can_repair_discovery_text_without_repeating_effective_plan():
    model = Sequence(
        scope(), wireless_plan(search_text=None),
        {'verdict': 'revise', 'reason': '遗漏机械键盘类型，应保留在search_text'},
        wireless_plan(), {'verdict': 'approve'},
    )
    plan = await CatalogSearchPlanner(model).plan(CatalogSearchRequest(
        query='推荐几款 500 元以内的无线机械键盘',
    ))
    assert plan.search_text == '机械键盘'
    assert len(model.calls) == 5
    assert json.loads(model.calls[-1][1].content)['candidate_plan']['search_text'] == '机械键盘'


@pytest.mark.asyncio
async def test_actual_repeated_rejected_plan_still_stops():
    model = Sequence(scope(), wireless_plan(search_text=None),
                     {'verdict': 'revise', 'reason': '遗漏机械键盘类型'},
                     wireless_plan(search_text=None))
    with pytest.raises(ModelCallError, match='Correction repeated a rejected plan'):
        await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query='推荐无线机械键盘'))
    assert len(model.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['search', 'lookup'])
async def test_named_identity_is_resolved_before_detail_and_cannot_be_replaced(operation):
    repository = IdentityRows([row(1, 10), row(2, 10), row(3, 20)])
    model = Model(scope(scope_kind='object', category=None, identity_text='测试键盘10'),
                  search_plan({'operation': operation, 'search_text': '测试键盘20',
                               'target': {'kind': 'spu', 'id': 20}, 'category': '鼠标',
                               'requested_fields': ['price']}))
    planner = CatalogSearchPlanner(model, operation=operation)
    plan, prepared = await planner.plan_with_repository(
        CatalogSearchRequest(query='查询测试键盘10的价格'), repository,
    )
    assert plan.search_text == '测试键盘10'
    assert plan.target is None and plan.category is None
    assert prepared.categories == ('键盘',)
    assert {r.product.sku_id for r in prepared.identity.records} == {1, 2}
    reviewed = json.loads(model.review_calls[0][1].content)
    assert reviewed['candidate_plan']['search_text'] == '测试键盘10'
    assert reviewed['original_request']['resolved_scope']['identity_count'] == 2
    execution = await CatalogSearchEngine(repository).execute(
        CatalogSearchRequest(query='查询测试键盘10的价格'), plan, identity=prepared.identity,
    )
    assert {r.product.sku_id for r in execution.selection.records} == {1, 2}


@pytest.mark.asyncio
async def test_explicit_id_without_name_cannot_acquire_a_different_identity_in_detail():
    target = {'kind': 'spu', 'id': 10}
    model = Model(scope(scope_kind='spu', target=target), search_plan({
        'search_text': '测试键盘20', 'target': {'kind': 'spu', 'id': 20},
    }))
    plan, prepared = await CatalogSearchPlanner(model).plan_with_repository(
        CatalogSearchRequest(query='从SPU10推荐一个版本'), IdentityRows([row(1, 10), row(2, 20)]),
    )
    assert plan.search_text is None and plan.target.model_dump() == target
    assert {r.product.sku_id for r in prepared.identity.records} == {1}


@pytest.mark.asyncio
@pytest.mark.parametrize('target,name,request_scope,status', [
    ({'kind': 'spu', 'id': 10}, '完全不同的鼠标', None, 'identity_mismatch'),
    ({'kind': 'spu', 'id': 10}, '测试键盘10', {'spu_ids': [20]}, 'outside_scope'),
    (None, '测试键盘10', {'spu_ids': [20]}, 'outside_scope'),
])
async def test_identity_conflicts_stop_before_detailed_planning(
    target, name, request_scope, status,
):
    model = Model(scope(scope_kind='object', identity_text=name, target=target))
    request = CatalogSearchRequest(query='查询指定商品价格', **(
        {'scope': request_scope} if request_scope else {}
    ))
    _, prepared = await CatalogSearchPlanner(model).plan_with_repository(
        request, IdentityRows([row(1, 10), row(2, 20)]),
    )
    assert prepared.identity.records == []
    assert prepared.identity.object_results[0].status == status
    assert len(model.calls) == 1 and not model.review_calls


@pytest.mark.asyncio
async def test_facets_keeps_named_object_boundary():
    model = Model(scope(scope_kind='object', identity_text='测试键盘10'),
                  facet_plan({'selection': 'object', 'name': '测试键盘20'}))
    plan = await CatalogFacetPlanner(model).plan_facets(CatalogFacetInput(query='测试键盘10参数'))
    assert plan.name == '测试键盘10'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,path,error_type', [
    ('missing', ['candidate_unit'], 'SCHEMA_ERROR'),
    ('nested', ['conditions', 0, 'unexpected'], 'SCHEMA_ERROR'),
    ('operator', ['conditions', 0, 'operator'], 'RULE_ERROR'),
])
async def test_detailed_plan_still_requires_structure_and_hard_rules(fault, path, error_type):
    invalid = wireless_plan()
    if fault == 'missing':
        invalid.pop('candidate_unit')
    elif fault == 'nested':
        invalid['conditions'][0]['unexpected'] = 'must reject'
    else:
        invalid['conditions'][0]['operator'] = 'lte'  # Text field cannot use numeric comparison.
    model = Model(scope(), invalid, wireless_plan())
    plan = await CatalogSearchPlanner(model).plan(CatalogSearchRequest(
        query='推荐几款 500 元以内的无线机械键盘',
    ))
    assert plan.search_text == '机械键盘'
    assert len(model.calls) == 3 and len(model.review_calls) == 1
    feedback = json.loads(model.calls[-1][-1].content)['validation_feedback']
    assert any(e['path'] == path and e['type'] == error_type for e in feedback['errors'])
