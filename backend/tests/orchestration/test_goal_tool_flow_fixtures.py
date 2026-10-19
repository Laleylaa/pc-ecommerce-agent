"""Verify evaluation adapters against independent fixture expectations, no model/DB calls."""

import pytest

from app.schemas.catalog_search import CatalogSearchRequest
from app.tools.catalog import CatalogToolService
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.orders import LLMOrderQueryPlanner, OrderToolService
from app.tools.schemas import (
    CatalogFacetInput,
    DocumentSearchInput,
    OrderLookupInput,
)
from scripts.goal_tool_fixtures import (
    DenySession,
    OrderRows,
    SearchRecords,
    fixture_patches,
    knowledge_service,
)
from tests.support.tool_plan_fixtures import order_plan
from tests.support.tool_planning import Model


@pytest.mark.asyncio
async def test_fixture_search_reads_known_object_and_not_found():
    service = CatalogSearchService(SearchRecords())
    found = await service.search(CatalogSearchRequest(query="查SKU 101的价格"))
    assert found.products[0].price == 260
    absent = await service.search(CatalogSearchRequest(query="查SKU 999的价格"))
    assert absent.status == "not_found" and not absent.products


@pytest.mark.asyncio
async def test_fixture_facets_use_real_aggregation():
    with fixture_patches()[0]:
        service = CatalogToolService(DenySession())
        facets = await service.facets(
            CatalogFacetInput(query="", facet="spec_value", category="keyboard", spec_key="style")
        )
        assert {item.value for item in facets.items} == {"80%", "75%", "全尺寸"}


@pytest.mark.asyncio
async def test_fixture_order_keeps_logistics_and_rejects_other_identity():
    with fixture_patches()[1]:
        output = await OrderToolService(
            DenySession(), planner=LLMOrderQueryPlanner(Model(order_plan({
                "selection": "recent", "requested_fields": ["order_summary"],
            }))),
        ).lookup(
            OrderLookupInput(
                user_id=1,
                query="查询最近订单和物流状态",
            )
        )
        assert output.returned_count == 1
        assert output.candidates[0].latest_logistics_event["description"] == "测试包裹运输中"
    with pytest.raises(AssertionError):
        await OrderRows(DenySession()).count_orders(2)


@pytest.mark.asyncio
async def test_fixture_policy_retrieval_returns_source_evidence_without_embedding_io():
    output = await knowledge_service().search_policy(
        DocumentSearchInput(query="键盘拆封七天无理由退货")
    )
    assert output.documents and output.documents[0].source_id == 8001
    assert "拆封" in output.documents[0].snippet
