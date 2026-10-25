"""Regression checks for independent evaluation oracles, not production business proof."""

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def basis(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    return importlib.import_module("catalog_basis")


def record(sku, spu, price, *, category="键盘", title=None, sales=0, **specs):
    return {
        "sku_id": sku, "spu_id": spu, "sku_title": f"独立版本{sku}",
        "spu_title": title or f"独立型号{spu}", "brand": "Keychron", "category": category,
        "price": str(price), "stock": sku, "sku_status": 1, "spu_status": 1,
        "specs_json": specs, "attributes": [], "spu_sales_count": sales, "sku_sales_count": 0,
    }


@pytest.mark.contract
def test_oracle_does_not_join_constraints_across_versions(basis):
    rows = [
        record(1, 1, 100, color="黑色", wireless="否", switches="线性红轴"),
        record(2, 1, 110, color="白色", wireless="是", switches="线性红轴"),
        record(3, 2, 200, color="黑色", wireless="是", switches="静音红轴"),
        record(5, 4, 501, color="黑色", wireless="是", switches="线性红轴"),
    ]
    actual = basis.derive("core_search_combination_black", rows)
    assert actual["spu_ids"] == [2]
    assert actual["selected_sku_ids"] == [3]
    assert actual["sku_count"] == actual["returned"] == 1


@pytest.mark.contract
def test_oracle_filters_before_ranking_and_defaults_to_three(basis):
    rows = [record(1, 1, 501, sales=999), record(2, 2, 500, sales=10),
            record(3, 3, 200, sales=30), record(4, 4, 300, sales=20),
            record(5, 5, 100, sales=1)]
    actual = basis.derive("core_search_sales", rows)
    assert actual["ordered_spu_ids"] == [3, 4, 2]
    assert actual["total"] == 4
    assert actual["returned"] == 3


@pytest.mark.contract
def test_oracle_preserves_numeric_endpoints_and_rejects_unknown_units(basis):
    rows = [record(1, 1, 300, category="鼠标", weight_g="60g", max_dpi="20000"),
            record(2, 2, 200, category="鼠标", weight_g="59", max_dpi="24000dpi"),
            record(3, 3, 100, category="鼠标", weight_g="61", max_dpi="30000")]
    assert basis.derive("core_search_mouse", rows)["spu_ids"] == [1, 2]
    rows[0]["specs_json"]["weight_g"] = "0.06kg"
    with pytest.raises(ValueError, match="unsupported_fixture_number"):
        basis.derive("core_search_mouse", rows)


@pytest.mark.contract
def test_oracle_lookup_full_population_and_distinct_price_levels(basis):
    title = "Keychron 星轴 K01 机械键盘"
    rows = [record(1, 1, 100, title=title), record(2, 1, 100, title=title),
            record(3, 1, 300, title=title), record(4, 1, 400, title=title)]
    rows[-1]["sku_status"] = 0
    actual = basis.derive("core_lookup_stock", rows)
    assert (actual["count"], actual["price_levels"], actual["maximum"]) == (3, 2, "300")
    assert actual["stock_values"] == {"1": [1], "2": [2], "3": [3]}


@pytest.mark.contract
def test_oracle_facet_dimensions_keep_each_values_sources(basis):
    rows = [record(1, 1, 100, color="白色", wireless="否"),
            record(2, 1, 200, color="黑色", wireless="是"),
            record(3, 2, 300, color="白色", wireless="是")]
    actual = basis.derive("core_facets_keys", rows)
    assert actual["facet_counts"]["color"] == {"sku_count": 3, "spu_count": 2, "count": 2}
    assert actual["dimension_values"]["color"] == {
        "白色": {"sku_ids": [1, 3], "spu_ids": [1, 2]},
        "黑色": {"sku_ids": [2], "spu_ids": [1]},
    }
    rows[0]["attributes"] = [{"key": "color", "value": "黑色"}]
    with pytest.raises(ValueError, match="complete_consistent_color"):
        basis.derive("core_facets_keys", rows)


@pytest.mark.contract
def test_oracle_facet_window_keeps_full_total(basis):
    rows = [record(i, i, 100) for i in range(1, 5)]
    for row, brand in zip(rows, ["A", "A", "B", "C"], strict=True):
        row["brand"] = brand
    actual = basis.derive("core_facets_window", rows)
    assert (actual["returned"], actual["total"], actual["exhaustive"]) == (2, 3, False)
    assert actual["facet_counts"] == {
        "A": {"sku_count": 2, "spu_count": 2, "count": 2},
        "B": {"sku_count": 1, "spu_count": 1, "count": 1},
    }


@pytest.mark.contract
def test_core_cases_are_unique_and_preannotated():
    tools = Path(__file__).resolve().parents[1]
    for tool, count in [("search", 12), ("lookup", 11), ("facets", 9)]:
        suite = json.loads((tools / f"catalog_{tool}" / "cases.json").read_text())
        cases = suite["cases"]
        assert len({c["id"] for c in cases}) == len(cases)
        assert sum(c["kind"] == "business" for c in cases) == count
        assert sum(c["kind"] == "boundary" for c in cases) == (0 if tool == "facets" else 2)
        for case in cases:
            assert case["semantic_expectation"] and case["data_requirement"]
            if case["kind"] == "contract":
                assert case["hard"]
            else:
                assert not case["hard"] and case["review_reference"]
