"""Small discriminating populations; independent expected IDs/prices, not live LLM tests."""
import pytest

from tests.tools.shared.catalog_business_support import condition, invoke, row

pytestmark = pytest.mark.asyncio


@pytest.mark.smoke
async def test_formal_search_returns_real_service_result(monkeypatch):
    out = await invoke(monkeypatch, 'search', [row(1)], {'category': '键盘'})
    assert out['products'][0]['sku_id'] == 1
    assert out['total_match_count'] == 1


@pytest.mark.business
async def test_price_and_color_must_match_same_sku(monkeypatch):
    out = await invoke(monkeypatch, 'search', [
        row(1, price=100, color='黑色'), row(2, price=400, color='白色'),
        row(3, spu=20, price=250, color='白色'),
    ], {'category': '键盘', 'conditions': [
        condition('color', ['白色']), condition('price', ['300'], 'lte'),
    ]})
    assert [(p['spu_id'], p['sku_id'], p['price']) for p in out['products']] == [(20, 3, '250')]
    assert out['matched_sku_count'] == 1


@pytest.mark.business
async def test_budget_excludes_sales_leader_before_ranking(monkeypatch):
    out = await invoke(monkeypatch, 'search', [
        row(1, spu=10, price=400, sales=999), row(2, spu=20, price=200, sales=30),
        row(3, spu=30, price=100, sales=50),
    ], {'category': '键盘', 'sort': 'sales',
        'conditions': [condition('price', ['300'], 'lte')]})
    assert [p['spu_id'] for p in out['products']] == [30, 20]


@pytest.mark.business
async def test_version_selection_does_not_collapse_to_spu(monkeypatch):
    out = await invoke(monkeypatch, 'search', [row(1, price=200), row(2, price=100)],
                       {'category': '键盘', 'candidate_unit': 'sku', 'sort': 'price_asc'},
                       scope={'spu_ids': [10]}, limit=2)
    assert [p['sku_id'] for p in out['products']] == [2, 1]
    assert out['selection_scope'] == 'sku'
    assert out['total_match_count'] == 2


@pytest.mark.business
async def test_soft_preference_ranks_without_excluding(monkeypatch):
    out = await invoke(monkeypatch, 'search', [
        row(1, spu=10, price=100, color='黑色'), row(2, spu=20, price=200, color='白色'),
    ], {'category': '键盘', 'sort': 'price_asc',
        'conditions': [condition('color', ['白色'], strength='preference')]})
    assert [p['spu_id'] for p in out['products']] == [20, 10]
    assert out['matched_spu_count'] == 2


@pytest.mark.boundary
async def test_complete_catalog_absence_satisfies_exclusion(monkeypatch):
    out = await invoke(monkeypatch, 'search', [row(1), row(2, spu=20, color='白色')],
                       {'category': '键盘', 'conditions': [condition('color', ['黑色'], 'ne')]})
    assert [p['sku_id'] for p in out['products']] == [1, 2]
    assert out['unknown_sku_count'] == 0
    assert out['matching_complete'] is True


@pytest.mark.boundary
async def test_alternative_never_escapes_trusted_scope(monkeypatch):
    out = await invoke(monkeypatch, 'search', [
        row(1, spu=10, color='黑色'), row(2, spu=20, color='白色'),
    ], {'category': '键盘', 'conditions': [condition('color', ['白色'])]},
                       scope={'spu_ids': [10]})
    assert out['matched_sku_count'] == 0
    assert out['match_kind'] == 'alternative'
    assert [p['sku_id'] for p in out['products']] == [1]
    assert out['products'][0]['condition_checks'][0]['verdict'] == 'mismatch'


@pytest.mark.business
async def test_requested_count_caps_larger_public_window_and_nested_versions(monkeypatch):
    out = await invoke(monkeypatch, 'search', [
        row(i, spu=10+(i-1)//3, price=100+i, color=f'颜色{i}') for i in range(1, 13)
    ], {'category': '键盘', 'sort': 'price_asc', 'requested_limit': 8},
       requested_count=3, limit=10)
    assert len(out['products']) == 3
    assert sum(len(p['candidate_sku_ids']) for p in out['products']) == 3
    assert all(len(p['series_variants']) == 1 for p in out['products'])
    assert all(not p['specs'] and not p['series_option_specs'] for p in out['products'])
    assert out['matched_sku_count'] == 12
