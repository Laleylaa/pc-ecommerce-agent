"""Object facts, subset populations and non-fabrication through the formal tool boundary."""
import pytest
from app.schemas.catalog_search import FieldIssue

from tests.tools.shared.catalog_business_support import condition, invoke, row

pytestmark = pytest.mark.asyncio


def plan(**changes):
    return {'search_text': '测试键盘10', 'version_view': 'list',
            'requested_fields': ['price', 'stock'], **changes}


@pytest.mark.smoke
async def test_formal_lookup_returns_version_facts(monkeypatch):
    out = await invoke(monkeypatch, 'lookup', [row(1, price=123, stock=7)], plan())
    assert out['products'][0]['price'] == '123'
    assert out['products'][0]['stock'] == 7


@pytest.mark.business
async def test_budget_changes_matching_population(monkeypatch):
    out = await invoke(monkeypatch, 'lookup', [
        row(1, price=199), row(2, price=300), row(3, price=301),
    ], plan(conditions=[condition('price', ['300'], 'lte')], sort='price_asc'))
    assert [p['sku_id'] for p in out['products']] == [1, 2]
    summary = out['lookup_summary']
    assert (summary['matched_sku_count'], summary['min_price'], summary['max_price']) == (
        2, '199', '300',
    )


@pytest.mark.business
async def test_window_preserves_full_price_stock_population(monkeypatch):
    out = await invoke(monkeypatch, 'lookup', [
        row(1, price=100, stock=2), row(2, price=200, stock=3), row(3, price=400, stock=4),
    ], plan(sort='price_asc'), limit=2)
    assert len(out['products']) == 2
    assert out['is_exhaustive'] is False
    summary = out['lookup_summary']
    assert summary['matched_sku_count'] == 3 and summary['max_price'] == '400'
    assert summary['fields'] == []
    assert summary['representative_versions'] == []
    assert [p['stock'] for p in out['products']] == [2, 3]


@pytest.mark.business
async def test_false_fact_is_returned_without_positive_filter(monkeypatch):
    out = await invoke(monkeypatch, 'lookup', [row(1, wireless='否'), row(2, wireless='是')],
                       plan(version_view='summary', requested_fields=['wireless']))
    field = next(f for f in out['lookup_summary']['fields'] if f['field'] == 'wireless')
    assert {v['value']: {r['sku_id'] for r in v['versions']} for v in field['values']} == {
        '否': {1}, '是': {2},
    }


@pytest.mark.business
async def test_zero_stock_is_not_inactive(monkeypatch):
    out = await invoke(monkeypatch, 'lookup', [row(1, stock=0), row(2, active=False)], plan())
    assert [p['sku_id'] for p in out['products']] == [1]
    assert out['products'][0]['stock'] == 0
    assert out['lookup_summary']['matched_sku_count'] == 1


@pytest.mark.boundary
async def test_absent_boolean_is_false_but_conflicting_fact_stays_unknown(monkeypatch):
    conflicting = row(2, wireless='是')
    conflicting.issues = [FieldIssue(sku_id=2, field='wireless', status='conflict')]
    out = await invoke(monkeypatch, 'lookup', [row(1), conflicting],
                       plan(version_view='summary', requested_fields=['wireless']))
    field = next(f for f in out['lookup_summary']['fields'] if f['field'] == 'wireless')
    assert [v['value'] for v in field['values']] == ['否']
    assert field['values'][0]['versions'][0]['sku_id'] == 1
    assert field['missing_sku_count'] == 0
    assert field['conflicted_sku_count'] == 1


@pytest.mark.boundary
async def test_named_object_outside_scope_has_no_price(monkeypatch):
    out = await invoke(monkeypatch, 'lookup', [row(1), row(2, spu=20)], plan(),
                       scope={'spu_ids': [20]})
    assert out['reason_code'] == 'target_outside_scope'
    assert out['products'] == [] and out['lookup_summary'] is None


@pytest.mark.business
async def test_list_variants_does_not_expand_unasked_specs_or_extra_candidates(monkeypatch):
    out = await invoke(monkeypatch, 'lookup', [
        row(i, price=100+i, color=f'颜色{i}', switches='线性红轴') for i in range(1, 13)
    ], plan(requested_fields=['price', 'variants'], sort='price_asc'), limit=2)
    assert [p['sku_id'] for p in out['products']] == [1, 2]
    assert all(p['specs'] == {} for p in out['products'])
    assert out['lookup_summary']['matched_sku_count'] == 12
    assert out['lookup_summary']['fields'] == []
    assert out['lookup_summary']['representative_versions'] == []
