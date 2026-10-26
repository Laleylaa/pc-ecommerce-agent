"""Aggregation counts and provenance on deliberately overlapping SKU/SPU populations."""
import pytest
from app.schemas.catalog_search import FieldIssue

from tests.tools.shared.catalog_business_support import condition, invoke, row

pytestmark = pytest.mark.asyncio


def plan(**changes):
    return {'facet': 'spec_value', 'spec_key': 'color', 'count_scope': 'spu', **changes}


@pytest.mark.smoke
async def test_formal_facets_returns_recorded_options(monkeypatch):
    out = await invoke(monkeypatch, 'facets', [row(1, color='白色')], plan())
    assert [item['value'] for item in out['items']] == ['白色']
    assert out['matching_complete'] is True


@pytest.mark.business
async def test_same_value_counts_skus_and_distinct_spus(monkeypatch):
    out = await invoke(monkeypatch, 'facets', [
        row(1, color='白色'), row(2, color='白色'), row(3, spu=20, color='白色'),
    ], plan())
    item = out['items'][0]
    assert (item['count'], item['spu_count'], item['sku_count']) == (2, 2, 3)
    assert item['values'][0]['sku_ids'] == []
    assert item['values'][0]['total_sku_ids_count'] == 3
    assert item['values'][0]['spu_ids'] == []
    assert item['values'][0]['total_spu_ids_count'] == 2


@pytest.mark.business
async def test_conditions_apply_before_aggregation_on_same_sku(monkeypatch):
    out = await invoke(monkeypatch, 'facets', [
        row(1, price=100, color='黑色', switches='线性红轴'),
        row(2, price=400, color='白色', switches='段落茶轴'),
        row(3, spu=20, price=200, color='白色', switches='磁轴'),
    ], plan(spec_key='switches', conditions=[
        condition('color', ['白色']), condition('price', ['300'], 'lte'),
    ]))
    assert [item['value'] for item in out['items']] == ['磁轴']
    assert out['items'][0]['values'][0]['total_sku_ids_count'] == 1


@pytest.mark.business
async def test_sku_scope_does_not_expand_to_sibling_versions(monkeypatch):
    out = await invoke(monkeypatch, 'facets', [row(1, color='黑色'), row(2, color='白色')],
                       plan(), scope={'sku_ids': [1]})
    assert [item['value'] for item in out['items']] == ['黑色']
    assert out['items'][0]['sku_count'] == 1


@pytest.mark.business
async def test_window_keeps_full_option_total(monkeypatch):
    out = await invoke(monkeypatch, 'facets', [
        row(1, color='白色'), row(2, spu=20, color='白色'), row(3, spu=30, color='黑色'),
    ], plan(), limit=1)
    assert [item['value'] for item in out['items']] == ['白色']
    assert out['total_value_count'] == 2 and out['returned_count'] == 1
    assert out['is_exhaustive'] is False and out['matching_complete'] is True


@pytest.mark.boundary
async def test_absent_option_does_not_make_complete_catalog_unknown(monkeypatch):
    out = await invoke(monkeypatch, 'facets', [row(1), row(2, color='白色')], plan())
    assert [item['value'] for item in out['items']] == ['白色']
    assert out['matching_complete'] is True and out['is_exhaustive'] is True
    assert out['field_issues'] == []


@pytest.mark.boundary
async def test_conflicting_value_is_not_counted_as_confirmed(monkeypatch):
    conflicting = row(1, color='白色')
    conflicting.issues = [FieldIssue(sku_id=1, field='color', status='conflict')]
    out = await invoke(monkeypatch, 'facets', [conflicting, row(2, color='黑色')], plan())
    assert [item['value'] for item in out['items']] == ['黑色']
    assert out['items'][0]['values'][0]['total_sku_ids_count'] == 1
    assert out['matching_complete'] is False


@pytest.mark.business
async def test_requested_option_count_caps_public_window(monkeypatch):
    out = await invoke(monkeypatch, 'facets', [
        row(i, color=f'颜色{i}') for i in range(1, 6)
    ], plan(limit=3), limit=10)
    assert len(out['items']) == 3 and out['total_value_count'] == 5
    assert all(not v['sku_ids'] and not v['spu_ids']
               for item in out['items'] for v in item['values'])
