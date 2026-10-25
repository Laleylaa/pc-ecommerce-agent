"""Check preparation and trace integrity; semantic correctness is externally reviewed."""

import copy
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    return importlib.import_module("catalog_basis"), importlib.import_module("check_catalog_cases")


def catalog_rows():
    rows = []
    for sku, color, switches, wireless in [
        (1, "黑色", "线性红轴", "是"), (2, "白色", "段落茶轴", "是"),
        (3, "粉色", "磁轴", "是"),
    ]:
        rows.append({
            "sku_id": sku, "spu_id": 10, "sku_title": f"独立版本{sku}",
            "spu_title": "Keychron 星轴 K01 机械键盘", "brand": "Keychron", "category": "键盘",
            "sku_status": 1, "spu_status": 1, "price": "219.00", "stock": 7,
            "specs_json": {"color": color, "switches": switches, "wireless": wireless},
            "attributes": [],
        })
    return rows


@pytest.mark.contract
def test_zero_matches_and_absent_combination_are_executable(modules):
    basis, _ = modules
    rows = catalog_rows()
    search = basis.derive("core_search_combination", rows)
    assert (search["total"], search["returned"], search["sku_count"]) == (0, 0, 0)
    assert {r["sku_id"] for r in search["alternative_material"]} == {1, 2}
    assert [r["mismatched_keys"] for r in search["alternative_material"]] == [
        ["color"], ["switches"],
    ]
    lookup = basis.derive("core_lookup_combination", rows)
    assert lookup["count"] == lookup["price_levels"] == 0
    assert lookup["minimum"] is lookup["maximum"] is None
    assert lookup["color_values"] == lookup["switches_values"] == {}
    assert lookup["spu_ids"] == [10]  # Object exists; the requested combination does not.


@pytest.mark.contract
def test_missing_or_conflicting_data_does_not_become_no_match(modules):
    basis, _ = modules
    rows = catalog_rows()
    rows[1]["attributes"] = [{"key": "switches", "value": "红轴"}]
    with pytest.raises(ValueError, match="complete_consistent_switches"):
        basis.derive("core_lookup_combination", rows)
    rows[1]["attributes"] = []
    del rows[1]["specs_json"]["switches"]
    with pytest.raises(ValueError, match="complete_consistent_switches"):
        basis.derive("core_search_combination", rows)


@pytest.mark.contract
def test_ambiguous_name_yields_candidates_instead_of_failed_preparation(modules):
    basis, _ = modules
    rows = [catalog_rows()[0]]
    second = copy.deepcopy(rows[0])
    second.update(sku_id=2, spu_id=20, brand="Akko", spu_title="Akko 星轴 K01 机械键盘")
    rows.append(second)
    oracle = basis.derive("core_lookup_partial", rows)
    assert {r["spu_id"] for r in oracle["identity_candidates"]} == {10, 20}
    assert all("price" not in r for r in oracle["identity_candidates"])


@pytest.mark.contract
def test_only_frozen_identity_pending_route_allows_one_model_stage(modules, monkeypatch):
    _, checker = modules
    result = {"output": {"status": "clarification_required"}}
    execution = {
        "trace_id": "t", "batch_id": "b", "case_id": "c", "attempt": 1,
        "tool_name": "catalog_lookup", "arguments": {"query": "星轴 K01多少钱"},
        "result": result, "tool_attempts": 1, "budget_calls": {"tool_planning": 1},
        "usage": {"http_attempts": 1, "attempt_stages": ["tool_planning"]},
    }
    evidence = {
        "roots": [{k: execution[k] for k in ("trace_id", "batch_id", "case_id", "attempt")}],
        "tools": [{"name": "catalog_lookup", "input": execution["arguments"], "output": result}],
        "models": [{"span_id": "m", "input": {}, "output": {},
                    "input_truncated": False, "output_truncated": False}],
        "validations": [
            {"schema": "CatalogScopePlan", "validation": "计划校验", "outcome": "accepted",
             "model_span_id": "m"},
            {"schema": "CatalogSearchResult", "validation": "工具输出校验",
             "outcome": "accepted", "input": result["output"]},
        ],
        "events": [{"name": "程序 · 商品范围准备"}],
    }
    monkeypatch.setattr(checker, "extract", lambda _: evidence)
    response = {"data": [{"context": {"trace_id": "t", "span_id": "m"}}]}
    rows, _ = checker.trace_rows({"trace_route": "identity_pending"}, execution, response)
    assert all(r["status"] == "通过" for r in rows)
    rows, _ = checker.trace_rows({"trace_route": "model"}, execution, response)
    assert next(r for r in rows if r["field"] == "models_complete")["status"] == "无法判定"
