"""Normal shopping through real tools; scripted extraction does not test live NL understanding."""

import pytest
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool

from tests.support.tool_plan_fixtures import facet_plan, search_plan
from tests.support.tool_planning import Model, Repository, product


def condition(key, values, evidence, operator="eq"):
    return {"key": key, "operator": operator, "values": values, "evidence": evidence}


@pytest.fixture
def goods():
    rows = [
        product(1, 1, category="鼠标", price="180", connection_type="三模", color="白色"),
        product(2, 2, category="鼠标", price="120", connection_type="蓝牙", color="黑色"),
        product(3, 3, category="鼠标", price="90", connection_type="2.4G", color="白色"),
        product(4, 4, category="鼠标", price="60", connection_type="有线", color="白色"),
        # Separate versions cannot lend their capabilities to each other.
        product(5, 5, category="鼠标", price="220", connection_type="蓝牙", color="白色"),
        product(6, 5, category="鼠标", price="210", connection_type="2.4G", color="白色"),
    ]
    for row in rows:
        row.product.brand = f"品牌{row.product.spu_id}"
    return Repository(rows)


async def call_tool(monkeypatch, goods, tool, query, *plans):
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: goods.load(category, scope),
    )
    wire_plan = search_plan if tool == "catalog_search" else facet_plan
    model = Model(*(wire_plan(plan) for plan in plans))
    handler = (
        CatalogSearchService(goods, CatalogSearchPlanner(model)).search
        if tool == "catalog_search"
        else CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets
    )
    result = await execute_bound_tool(
        BoundTool(DefaultToolContractProvider().get_contract(tool), handler),
        {"query": query, "limit": 20}, {"user_id": 7},
    )
    assert result.ok, result.error
    return result.output, model


SHOPPING_CASES = [
    ("有没有既支持蓝牙又支持2.4G的鼠标", [
        condition("connection_type", ["蓝牙"], "支持蓝牙"),
        condition("connection_type", ["2.4G"], "支持2.4G"),
    ], {1}),
    ("找支持蓝牙的三模鼠标", [
        condition("connection_type", ["蓝牙"], "支持蓝牙"),
        condition("connection_type", ["三模"], "三模"),
    ], {1}),
    ("蓝牙或者2.4G连接的鼠标都可以", [
        condition("connection_type", ["蓝牙", "2.4G"], "蓝牙或者2.4G", "in"),
    ], {1, 2, 3, 5, 6}),
    ("想要无线鼠标，但不要支持蓝牙的", [
        condition("connection_type", ["无线"], "无线"),
        condition("connection_type", ["蓝牙"], "不要支持蓝牙", "ne"),
    ], {3, 6}),
    ("找200元以内的白色蓝牙鼠标", [
        condition("price", ["200"], "200元以内", "lte"),
        condition("color", ["白色"], "白色"),
        condition("connection_type", ["蓝牙"], "蓝牙"),
    ], {1}),
    ("鼠标要能插线用，也能用蓝牙", [
        condition("connection_type", ["有线"], "能插线用"),
        condition("connection_type", ["蓝牙"], "能用蓝牙"),
    ], {1}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
@pytest.mark.parametrize("query,conditions,expected_skus", SHOPPING_CASES)
async def test_normal_connection_shopping(
    monkeypatch, goods, tool, query, conditions, expected_skus,
):
    if tool == "catalog_facets":
        query += "，有哪些品牌？"
    output, model = await call_tool(
        monkeypatch, goods, tool, query, {"category": "鼠标", "conditions": conditions},
    )
    assert len(model.calls) == 1 and goods.calls
    if tool == "catalog_search":
        assert output["match_kind"] == "exact"
        assert {sku for p in output["products"] for sku in p["candidate_sku_ids"]} == expected_skus
        assert output["matched_sku_count"] == len(expected_skus)
    else:
        expected_brands = {f"品牌{5 if sku == 6 else sku}" for sku in expected_skus}
        assert {item["value"] for item in output["items"]} == expected_brands
        assert sum(item["sku_count"] for item in output["items"]) == len(expected_skus)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_office_and_charging_keep_separate_reasons(monkeypatch, goods, tool):
    output, _ = await call_tool(
        monkeypatch, goods, tool, "适合办公又支持无线充电的鼠标，200元以内，有哪些品牌？", {
            "category": "鼠标",
            "conditions": [condition("price", ["200"], "200元以内", "lte")],
            "unresolved_requirements": [
                {"evidence": "适合办公", "reason": "requirement_needs_clarification"},
                {"evidence": "支持无线充电", "reason": "filter_data_unavailable"},
            ],
        },
    )
    assert not goods.calls
    items = output["diagnostics"][0]["details"]["requirement_assessment"]["items"]
    assert {item["source"]: item["reasons"] for item in items} == {
        "200元以内": [], "适合办公": ["requirement_needs_clarification"],
        "支持无线充电": ["filter_data_unavailable"],
    }
    assert not output["matching_complete"] and not output["is_exhaustive"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_office_misclassification_repaired_without_losing_charging(monkeypatch, goods, tool):
    charging = {"evidence": "支持无线充电", "reason": "filter_data_unavailable"}
    output, model = await call_tool(
        monkeypatch, goods, tool, "适合办公又支持无线充电的鼠标有哪些品牌", {
            "unresolved_requirements": [charging],
            "clarification_requirements": ["适合办公"],
            "unsupported_requirements": ["适合办公"],
        }, {
            "unresolved_requirements": [
                charging, {"evidence": "适合办公", "reason": "requirement_needs_clarification"},
            ],
        },
    )
    assert len(model.calls) == 2 and not goods.calls
    items = output["diagnostics"][0]["details"]["requirement_assessment"]["items"]
    assert len(items) == 2
    assert {tuple(item["reasons"]) for item in items} == {
        ("filter_data_unavailable",), ("requirement_needs_clarification",),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("query,name,reason", [
    ("Logitech 影刃 M01有哪些规格", "Logitech 影刃 M01", "product_identity_required"),
    ("这款有哪些规格", None, "query_needs_clarification"),
])
async def test_unresolved_product_specs_are_not_complete(monkeypatch, goods, query, name, reason):
    output, _ = await call_tool(monkeypatch, goods, "catalog_facets", query, {
        "selection": "object", "facet": "spec_key", "name": name,
        "supported": False, "unsupported_reason": "缺少已定位对象",
    })
    assert not goods.calls
    assert output["diagnostics"][0]["code"] == reason
    assert not output["matching_complete"] and not output["is_exhaustive"]


@pytest.mark.asyncio
async def test_connection_options_preserve_recorded_modes(monkeypatch, goods):
    output, _ = await call_tool(
        monkeypatch, goods, "catalog_facets", "鼠标有哪些连接方式可以选", {
            "category": "鼠标", "facet": "spec_value", "spec_key": "connection_type",
        },
    )
    assert {item["value"]: item["sku_count"] for item in output["items"]} == {
        "三模": 1, "蓝牙": 2, "2.4G": 2, "有线": 1,
    }


@pytest.mark.asyncio
async def test_product_reference_clarification_is_not_a_use_case_question(monkeypatch, goods):
    output, _ = await call_tool(
        monkeypatch, goods, "catalog_facets", "这款商品都有什么规格", {
            "selection": "object", "facet": "spec_key",
            "unresolved_requirements": [
                {"evidence": "这款商品", "reason": "requirement_needs_clarification"},
            ],
        },
    )
    assert not goods.calls
    diagnostic = output["diagnostics"][0]
    assert diagnostic["code"] == "query_needs_clarification"
    assert diagnostic["recommended_action"] == "ask_user_to_identify_product"
    assert not output["matching_complete"] and not output["is_exhaustive"]
