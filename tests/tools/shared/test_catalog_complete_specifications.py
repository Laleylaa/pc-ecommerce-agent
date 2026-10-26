"""Complete catalog absence, subjective clarification and truthful alternative evidence."""

import pytest
from app.schemas.catalog_search import FieldIssue
from tools.shared.catalog_business_support import condition, invoke, row

pytestmark = pytest.mark.asyncio


async def test_absent_spec_keeps_budget_and_returns_only_labelled_alternatives(monkeypatch):
    out = await invoke(monkeypatch, 'search', [
        row(1, price=100), row(2, spu=20, price=250),
    ], {
        'category': '键盘', 'conditions': [condition('price', ['200'], 'lte')],
        'unresolved_requirements': [
            {'evidence': '支持无线充电', 'reason': 'spec_not_carried'},
        ],
    })
    assert out['match_kind'] == 'alternative'
    assert out['matched_sku_count'] == 0 and out['unknown_sku_count'] == 0
    assert [p['sku_id'] for p in out['products']] == [1]
    checks = out['products'][0]['condition_checks']
    assert [(c['condition']['evidence'], c['verdict']) for c in checks][-1] == (
        '支持无线充电', 'mismatch',
    )
    assert checks[-1]['actual'] == '否'


async def test_missing_boolean_does_not_become_unknown_or_satisfy_positive(monkeypatch):
    out = await invoke(monkeypatch, 'lookup', [row(1)], {
        'conditions': [condition('wireless', ['是'])],
    }, scope={'sku_ids': [1]})
    assert out['lookup_summary']['exists'] is False
    assert out['unknown_sku_count'] == 0
    assert out['match_kind'] == 'alternative'


async def test_absent_optional_preference_does_not_exclude_exact_candidates(monkeypatch):
    out = await invoke(monkeypatch, 'search', [row(1)], {
        'category': '键盘', 'unresolved_requirements': [
            {'evidence': '最好支持无线充电', 'reason': 'spec_not_carried',
             'strength': 'preference'},
        ],
    })
    assert out['match_kind'] == 'exact'
    assert out['matched_sku_count'] == 1
    assert out['products'][0]['condition_checks'][0]['verdict'] == 'mismatch'


async def test_subjective_usage_still_clarifies_instead_of_becoming_absent_spec(monkeypatch):
    out = await invoke(monkeypatch, 'search', [row(1)], {
        'category': '键盘', 'unresolved_requirements': [
            {'evidence': '办公使用', 'reason': 'requirement_needs_clarification'},
        ],
    })
    assert out['status'] == 'clarification_required'
    assert out['query_state'] == 'not_executed'
    assert out['products'] == []


async def test_conflicting_record_remains_unknown_and_never_becomes_alternative(monkeypatch):
    record = row(1, wireless='是')
    record.issues = [FieldIssue(sku_id=1, field='wireless', status='conflict')]
    out = await invoke(monkeypatch, 'search', [record], {
        'category': '键盘', 'conditions': [condition('wireless', ['是'])],
    })
    assert out['unknown_sku_count'] == 1
    assert out['products'] == [] and out['match_kind'] == 'none'


async def test_facets_missing_capability_is_empty_not_unavailable(monkeypatch):
    out = await invoke(monkeypatch, 'facets', [row(1, color='白色')], {
        'category': '键盘', 'query': '支持无线充电的键盘有哪些颜色',
        'facet': 'spec_value', 'spec_key': 'color',
        'unresolved_requirements': [
            {'evidence': '支持无线充电', 'reason': 'spec_not_carried'},
        ],
    })
    assert out['result_type'] == 'empty' and out['items'] == []
    assert out['matching_complete'] is True and out['is_exhaustive'] is True
