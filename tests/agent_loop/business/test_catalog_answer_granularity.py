"""Deterministic fallback and evidence transport; live prose is checked separately."""

from copy import deepcopy

import pytest
from app.agent.loop.independent_answer import fallback_answer, validate_answer

from evals.agent.answer_style_cases import catalog_granularity_cases, style_cases


def recommendation_facts():
    return deepcopy(catalog_granularity_cases()[0]['facts'])


def test_live_case_registry_constructs_with_current_tool_error_contract():
    # The CLI constructs the registry before filtering; stale unrelated fixtures
    # must not prevent these explicitly selected answer-only checks from starting.
    cases = style_cases()
    assert {'spu-recommendation', 'sku-recommendation', 'spu-candidate-stock'} <= {
        case['id'] for case in cases
    }


def test_spu_fallback_quotes_matching_ranges_instead_of_sample_or_whole_model():
    facts = recommendation_facts()
    draft = fallback_answer(facts)
    validate_answer(draft, facts)
    assert '测研 云杉 K7 机械键盘，本次候选版本价格 219—319 元' in draft.response
    assert '测研 湖光 K8 机械键盘，本次候选版本价格 289—459 元' in draft.response
    for unwanted in ('白色标准版', '所示版本价格', '169', '599', '699', '库存'):
        assert unwanted not in draft.response
    # Handoff retains both distinct levels; fallback formatting must not mutate evidence.
    product = facts.evidence[0]['result']['output']['products'][0]
    assert product['price'] == '219' and product['stock'] == 7
    assert product['series_total_stock'] == 23
    assert product['series_variants'][0]['specs']['connection_type'] == '蓝牙'


@pytest.mark.parametrize('missing', ['series_min_price', 'series_max_price'])
def test_missing_spu_aggregate_does_not_fall_back_to_representative_price(missing):
    facts = recommendation_facts()
    output = facts.evidence[0]['result']['output']
    output['products'] = output['products'][:1]
    output['products'][0][missing] = None
    response = fallback_answer(facts).response
    assert '价格范围资料不完整' in response
    for price in ('219', '319', '169', '599'):
        assert price not in response


def test_equal_spu_range_uses_single_price_with_candidate_scope():
    facts = recommendation_facts()
    product = facts.evidence[0]['result']['output']['products'][0]
    product['series_max_price'] = '219'
    response = fallback_answer(facts).response
    assert '本次候选版本价格 219 元' in response
    assert '219—219' not in response


def test_sku_fallback_keeps_specific_version_price_even_with_model_aggregates():
    facts = catalog_granularity_cases()[1]['facts']
    # Explicit per-product scope outranks an inconsistent collection label.
    facts.evidence[0]['result']['output']['selection_scope'] = 'spu'
    draft = fallback_answer(facts)
    validate_answer(draft, facts)
    assert '测研 云杉 K7 黑色增强版，所示版本价格 319 元' in draft.response
    assert '219' not in draft.response and '169' not in draft.response


def test_collection_scope_supports_older_products_without_entity_scope():
    facts = recommendation_facts()
    for product in facts.evidence[0]['result']['output']['products']:
        del product['entity_scope']
    response = fallback_answer(facts).response
    assert '本次候选版本价格 219—319 元' in response
    assert '所示版本价格' not in response
