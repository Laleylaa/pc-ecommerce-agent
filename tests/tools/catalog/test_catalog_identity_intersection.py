import json

import pytest
from app.schemas.catalog_search import CatalogSearchRequest, SearchPlan
from app.tools.catalog_search.validation import validate_plan

from tests.support.catalog_catalog_identity_intersection import (
    invoke,
    plan_for,
)
from tests.support.tool_plan_fixtures import facet_plan, search_plan
from tests.support.tool_planning import product


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize("kind,name,expected", [
    ("spu", "K01", {1, 2}), ("spu", "K01 2", {2}), ("sku", "K01", {1}),
])
async def test_name_and_id_intersect_without_losing_version_granularity(
    monkeypatch, tool, kind, name, expected,
):
    rows = [product(1, color="白色"), product(2, color="黑色"), product(3, 2, color="粉色")]
    result, model, repository = await invoke(
        monkeypatch, tool, f"查询{name}（{kind.upper()} 1）的价格及规格", rows,
        plan_for(tool, name=name, kind=kind),
    )
    assert result["ok"], result
    output = result["output"]
    if tool == "catalog_search":
        assert output["status"] == "success"
        ids = {sku for p in output["products"] for sku in p["candidate_sku_ids"]}
        assert output["object_results"] == [{
            "name": name, "target": {"kind": kind, "id": 1}, "status": "found",
        }]
    else:
        ids = {sku for item in output["items"] for v in item["values"] for sku in v["sku_ids"]}
    assert ids == expected
    assert len(model.calls) == 1
    assert getattr(repository.calls[0][1], f"{kind}_ids") == [1]
    # Search may read this SKU's own SPU for separately labelled model background.
    assert all(scope.sku_ids == [1] or scope.spu_ids == [1] for _, scope in repository.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize("problem,expected", [
    ("missing", "target_not_found"), ("inactive", "target_inactive"),
    ("scope", "target_outside_scope"),
])
async def test_number_availability_not_confused_with_name_conflict(
    monkeypatch, tool, problem, expected,
):
    row = product(1, color="白色")
    row.active = problem != "inactive"
    result, model, _ = await invoke(
        monkeypatch, tool, "查询K02（SPU 1）的价格及规格",
        [] if problem == "missing" else [row], plan_for(tool, name="K02"),
        scope={"spu_ids": [2]} if problem == "scope" else None,
    )
    assert result["ok"], result
    assert result["output"]["diagnostics"][0]["code"] == expected
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize("problem", ["name", "target"])
@pytest.mark.parametrize("repair", [True, False])
async def test_invented_identity_repaired_before_business_classification(
    monkeypatch, tool, problem, repair,
):
    bad = plan_for(tool, name="K02" if problem == "name" else "K01",
                   identity=999 if problem == "target" else 1)
    good = plan_for(tool)
    result, model, repository = await invoke(
        monkeypatch, tool, "查询K01（SPU 1）的价格及规格", [product(1, color="白色")],
        bad, good if repair else bad,
    )
    assert len(model.calls) == 2
    feedback = json.loads(model.calls[1][-1].content)["validation_feedback"]
    assert feedback["errors"][0]["type"] == "RULE_ERROR"
    if repair:
        assert result["ok"] and len(repository.calls) >= 1
    else:
        assert result["error"]["code"] == "planning_output_invalid"
        assert not repository.calls


def test_bad_identity_not_hidden_by_unsupported_category():
    request = CatalogSearchRequest(query="查K01（SPU 1）手机的价格")
    plan = SearchPlan(name="K02", target={"kind": "spu", "id": 1}, category="手机")
    assert validate_plan(plan, request).reason == "invalid_plan"
    plan.name = "K01"
    assert validate_plan(plan, request).reason == "request_not_supported"


@pytest.mark.asyncio
async def test_actual_independent_objects_still_request_split(monkeypatch):
    result, model, repository = await invoke(
        monkeypatch, "catalog_search", "查询K01与SPU 2各自的价格", [],
        search_plan({"disposition": "split_required", "reason": "两个独立对象"}),
    )
    assert result["output"]["reason_code"] == "multiple_targets"
    assert len(model.calls) == 1 and not repository.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_combined_identity_keeps_hard_conditions_on_one_sku(monkeypatch, tool):
    conditions = [
        {"key": "color", "values": ["白色"], "evidence": "白色"},
        {"key": "price", "operator": "lte", "values": ["200"], "evidence": "200元以内"},
    ]
    fields = {"conditions": conditions}
    if tool == "catalog_search":
        fields["operation"] = "variants"
    result, _, _ = await invoke(
        monkeypatch, tool, "K01（SPU 1）里白色且200元以内的版本",
        [product(1, color="白色", price="100"), product(2, color="白色", price="300"),
         product(3, color="黑色", price="100"), product(4, 2, color="白色", price="100")],
        plan_for(tool, **fields),
    )
    assert result["ok"], result
    output = result["output"]
    if tool == "catalog_search":
        assert output["selection_scope"] == "sku"
        ids = {sku for p in output["products"] for sku in p["candidate_sku_ids"]}
    else:
        ids = {sku for item in output["items"] for v in item["values"] for sku in v["sku_ids"]}
    assert ids == {1}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [None, "K01"])
@pytest.mark.parametrize("problem,expected", [
    ("missing", "target_not_found"), ("inactive", "target_inactive"),
])
async def test_facet_scope_only_preserves_object_availability(monkeypatch, name, problem, expected):
    row = product(1, color="白色")
    row.active = problem != "inactive"
    result, _, _ = await invoke(
        monkeypatch, "catalog_facets", "查询K01的规格", [] if problem == "missing" else [row],
        facet_plan({"selection": "object", "facet": "spec_key", "name": name}),
        scope={"spu_ids": [1]},
    )
    assert result["ok"], result
    assert result["output"]["diagnostics"][0]["code"] == expected


@pytest.mark.asyncio
async def test_named_facet_outside_scope_not_reported_as_missing(monkeypatch):
    row = product(2, 2, color="白色")
    result, _, _ = await invoke(
        monkeypatch, "catalog_facets", "查询K01的规格", [row],
        facet_plan({"selection": "object", "facet": "spec_key", "name": "K01"}),
        scope={"spu_ids": [1]},
    )
    assert result["ok"], result
    assert result["output"]["diagnostics"][0]["code"] == "target_outside_scope"
