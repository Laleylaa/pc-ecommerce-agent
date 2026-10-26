import re

import pytest
from app.rules.order_matching import (
    item_name_parts,
    item_part_pattern,
    normalize_item_text,
)

from tests.support.orders_order_suggestions import lookup as lookup
from tests.support.orders_order_suggestions import order, september


@pytest.mark.parametrize("query,name", [
    ("测研茶语K1键盘", "测研 茶语 K1 机械键盘 黑色茶轴版"),
    ("Keychron K 8", "Keychron K8 Pro 机械键盘"),
    ("Ｋ１键盘", "测研 K1 键盘"),
])
def test_layout_differences_preserve_all_name_parts(query, name):
    assert all(re.search(item_part_pattern(p), normalize_item_text(name))
               for p in item_name_parts(query))


@pytest.mark.parametrize("name", ["K10键盘", "K1Pro键盘", "AK1键盘"])
def test_model_number_is_not_a_substring_identity(name):
    assert not re.search(item_part_pattern("K1"), normalize_item_text(name))


@pytest.mark.asyncio
async def test_similar_name_is_only_a_confirmation_candidate_and_preserves_owner_date(lookup):
    run, calls, rows = lookup
    rows.append((7, order(104, month=9, name="测研 茶语 K1 机械键盘")))
    rows.append((8, order(998, month=9, name="测研 茶语 K2 机械键盘")))
    result = await run(september().model_copy(update={"product_terms": ["测研茶语", "K2", "键盘"]}))
    assert result.result_type == "order_suggestions"
    assert [s.order.id for s in result.suggestions] == [104]
    assert result.total_match_count == result.returned_count == 0
    assert result.order is None and not result.focus_order_ids and not result.analysis_orders
    assert result.suggestions[0].differences[0].field == "product_terms"
    assert result.suggestions[0].differences[0].expected["product_terms"] == [
        "测研茶语", "K2", "键盘",
    ]
    assert all(uid == 7 for uid, _ in calls)
    assert calls[-1][1].start_at == september().start_at
