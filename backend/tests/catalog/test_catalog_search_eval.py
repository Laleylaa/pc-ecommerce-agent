"""Independent checks of scenario evaluation; no database or model calls."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.eval_catalog_search import verify


def sample_result(order):
    oracle = []
    products = {}
    for identity, price, sales, sku_sales in [(1, 200, 100, 1), (2, 100, 50, 99), (3, 100, 100, 2)]:
        row = {
            "sku_id": identity,
            "spu_id": identity,
            "price": Decimal(price),
            "sales_count": sales,
            "sku_sales_count": sku_sales,
            "title": f"型号{identity}白色版",
            "spu_title": f"型号{identity}",
            "brand": "测试品牌",
            "category": "键盘",
            "description": "已记录资料",
            "stock": 10,
            "image_url": None,
            "specs_json": {"color": "白色"},
        }
        oracle.append(row)
        variant = {**row, "price": str(price), "specs": row["specs_json"]}
        products[identity] = {
            **variant,
            "entity_scope": "spu",
            "candidate_sku_ids": [identity],
            "series_variants": [variant],
            "series_min_price": str(price),
            "series_max_price": str(price),
        }
    result = SimpleNamespace(
        ok=True,
        output={
            "status": "success",
            "query_state": "completed",
            "matched_sku_count": 3,
            "matched_spu_count": 3,
            "products": [products[i] for i in order],
        },
    )
    return result, oracle


@pytest.mark.parametrize(
    "sort,order",
    [("sales", [1, 3, 2]), ("price_asc", [2, 3, 1]), ("price_desc", [1, 2, 3])],
)
def test_evaluator_checks_model_sort_and_stable_ties(sort, order):
    case = {"expect": {"status": "success", "sort": sort}}
    result, oracle = sample_result(order)
    assert verify(case, result, oracle) == []
    result.output["products"].reverse()
    assert f"incorrect {sort} model window" in verify(case, result, oracle)


@pytest.mark.parametrize("keys", [[], ["color"], ["color", "stock"]])
def test_evaluator_catches_lost_or_added_condition_fields(keys):
    case = {"expect": {"condition_keys": ["color"]}}
    result = SimpleNamespace(
        ok=True, output={"query_plan": {"conditions": [{"key": key} for key in keys]}}
    )
    errors = verify(case, result, None)
    assert bool(errors) is (keys != ["color"])


def test_evaluator_checks_ranking_semantics_even_if_order_happens_to_match():
    case = {"expect": {"ranking_strategy": "spu_total_sales"}}
    result = SimpleNamespace(ok=True, output={"ranking_strategy": "relevance"})
    assert verify(case, result, None) == [
        "ranking_strategy: expected spu_total_sales, actual relevance"
    ]


@pytest.mark.parametrize(
    "operator,value,passes",
    [("eq", "无背光", True), ("ne", "true", True), ("ne", "false", False)],
)
def test_condition_probe_exposes_a_filter_hidden_by_correlated_catalog_data(
    operator, value, passes
):
    case = {"expect": {"condition_probes": [{"key": "backlit", "actual": "RGB", "matches": False}]}}
    result = SimpleNamespace(
        ok=True,
        output={
            "query_plan": {
                "conditions": [
                    {
                        "key": "backlit",
                        "operator": operator,
                        "values": [value],
                        "evidence": "不要背光",
                    }
                ]
            }
        },
    )
    assert (verify(case, result, None) == []) is passes


@pytest.mark.parametrize("key,passes", [("color", True), ("stock", False)])
def test_named_fact_queries_allow_identity_filters_but_not_inventory_filters(key, passes):
    case = {"expect": {"allowed_condition_keys": ["brand", "color"]}}
    result = SimpleNamespace(ok=True, output={"query_plan": {"conditions": [{"key": key}]}})
    assert (verify(case, result, None) == []) is passes


@pytest.mark.parametrize(
    "sort,order", [("sales", [2, 3, 1]), ("price_asc", [2, 3, 1]), ("price_desc", [1, 2, 3])]
)
def test_evaluator_checks_variant_window_with_one_model_and_sku_sales(sort, order):
    result, oracle = sample_result(order)
    for row in oracle:
        row.update(spu_id=33, spu_title="同一型号", sales_count=100)
    for product in result.output["products"]:
        product.update(spu_id=33, spu_title="同一型号", sales_count=100, entity_scope="sku")
    result.output.update(selection_scope="sku", matched_spu_count=1)
    case = {"expect": {"sort": sort}}
    assert verify(case, result, oracle) == []
    result.output["products"].reverse()
    assert f"incorrect {sort} sku window" in verify(case, result, oracle)


@pytest.mark.parametrize("hint", ["鼠标", "键盘", "耳机", "显示器", "音箱", "摄像头", None])
def test_current_suite_loads_hint_aware_public_inputs(tmp_path, hint):
    import json

    from scripts.eval_catalog_search import load_cases

    (tmp_path / "inputs").mkdir()
    case = {"id": "sample", "query": "找白色键盘", "args": {"category_hint": hint}, "expect": {}}
    path = tmp_path / "inputs/catalog_search_classic_cases.json"
    path.write_text(json.dumps([case]))
    loaded = load_cases("classic", root=tmp_path)
    assert loaded[0]["args"] == {"category_hint": hint}
    assert loaded[0]["suite"] == "classic"


@pytest.mark.parametrize("suite", ["user", "contract", "alignment", "scenarios", "specs", "hints"])
def test_retired_suite_names_do_not_silently_run_an_old_input_set(tmp_path, suite):
    from scripts.eval_catalog_search import load_cases

    with pytest.raises(ValueError, match="unknown_suite"):
        load_cases(suite, root=tmp_path)


@pytest.mark.parametrize("failure", ["duplicate", "invalid_hint", "empty"])
def test_suite_preflight_rejects_invalid_evidence_before_model_or_database(tmp_path, failure):
    import json

    from scripts.eval_catalog_search import load_cases

    (tmp_path / "inputs").mkdir()
    case = {"id": "sample", "query": "找键盘", "args": {"category_hint": "键盘"}, "expect": {}}
    if failure == "invalid_hint":
        case["args"]["category_hint"] = ["键盘"]
    cases = [] if failure == "empty" else [case, case] if failure == "duplicate" else [case]
    (tmp_path / "inputs/catalog_search_classic_cases.json").write_text(json.dumps(cases))
    with pytest.raises(ValueError):
        load_cases("classic", root=tmp_path)
