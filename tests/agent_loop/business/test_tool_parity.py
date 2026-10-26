"""P1 domain parity with independent facts; no real LLM or database acceptance."""

from copy import deepcopy

import pytest
from agent_loop.support.domain_tools import (
    TOOL_ARGUMENTS,
    adapter,
    admit_goal,
    make_catalog,
)
from app.agent.loop.evidence import EvidenceError
from app.agent.loop.tool_adapter import QueryToolAdapter
from app.tools.contracts import (
    BoundTool,
    ToolCatalog,
    ToolRuntimeContext,
    default_tool_contracts,
    execute_bound_tool,
)
from app.tools.orders import OrderToolService

pytestmark = [pytest.mark.business, pytest.mark.asyncio]


@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
async def test_five_formal_services_preserve_results_and_independent_facts(monkeypatch, name):
    catalog = make_catalog(monkeypatch)
    arguments = deepcopy(TOOL_ARGUMENTS[name])
    direct = await execute_bound_tool(
        catalog.get_by_llm_name(name), arguments, {"user_id": 41},
    )
    query = adapter(catalog)
    record = await query.execute(query.prepare("g1", name, arguments))
    assert record.result.ok, record.result.error
    assert record.result == direct
    output = record.result.output
    if name == "catalog_search":
        assert output["products"][0]["sku_id"] == 9101
        assert output["products"][0]["price"] == "299"
        assert output["products"][0]["specs"]["color"] == "白色"
        assert output["status"] == "success"
    elif name == "catalog_facets":
        assert output["facet"] == "spec_key"
        assert {i["value"] for i in output["items"]} >= {"color", "connection_type"}
    elif name == "order_lookup":
        assert output["order"]["id"] == 8201
        assert output["order"]["status_label"] == "已发货"
    elif name == "policy_search":
        assert {d["source_id"] for d in output["documents"]} == {7301}
        assert "全部配件" in output["documents"][0]["snippet"]
    else:
        assert output["documents"][0]["url"] == "https://example.test/p1/interfaces"
        assert output["documents"][0]["source_type"] == "web_page"


@pytest.mark.parametrize("user_id,order_id,expected", [
    (41, 8201, "single_order"), (42, 8201, "not_found"), (41, 8202, "not_found"),
])
async def test_two_users_and_foreign_orders_remain_isolated(
    monkeypatch, user_id, order_id, expected,
):
    query = QueryToolAdapter(
        make_catalog(monkeypatch), ToolRuntimeContext(user_id=user_id), admit_goal=admit_goal,
    )
    record = await query.execute(query.prepare("g1", "order_lookup", {"order_id": order_id}))
    assert record.result.output["result_type"] == expected
    assert bool(record.result.output["order"]) == (expected == "single_order")
    # A complete no-result lookup is not an execution failure or a found order.
    assert query.evidence.reference("g1", record.evidence_id, require_complete=True)


async def test_formal_order_repository_still_filters_trusted_user():
    statements = []

    class Session:
        async def execute(self, statement):
            statements.append(statement)
            class EmptyResult:
                def scalar_one_or_none(self):
                    return None
            return EmptyResult()

    # No repository monkeypatch here: inspect actual SQL produced by the real service/repository.
    session = Session()
    contract = next(c for c in default_tool_contracts() if c.llm_name == "order_lookup")
    query = adapter(ToolCatalog([BoundTool(contract, OrderToolService(session).lookup)]))
    record = await query.execute(query.prepare("g1", "order_lookup", {"order_id": 8201}))
    assert record.result.output["result_type"] == "not_found"
    assert len(statements) == 1
    compiled = statements[0].compile()
    assert compiled.params["user_id_1"] == 41
    assert compiled.params["id_1"] == 8201
    assert "user_id =" in str(compiled) and "id =" in str(compiled)


async def test_missing_product_is_not_fabricated_and_scope_is_preserved(monkeypatch):
    query = adapter(make_catalog(monkeypatch))
    arguments = {"query": "SKU 9999", "scope": {"sku_ids": [9101]}}
    record = await query.execute(query.prepare("g1", "catalog_search", arguments))
    output = record.result.output
    assert output["status"] == "not_found"
    assert output["reason_code"] == "target_not_found"
    assert output["products"] == []
    assert record.invocation.arguments["scope"]["sku_ids"] == [9101]
    assert query.evidence.presentation("g1", record.evidence_id)["candidates"]["products"] == []


async def test_window_never_becomes_exhaustive_evidence(monkeypatch):
    from agent_loop.support.domain_tools import FixtureSearchPlanner, product
    from app.schemas.catalog_search import SearchPlan

    async def plan(self, request):
        return SearchPlan(category="键盘", category_evidence="键盘")

    monkeypatch.setattr(FixtureSearchPlanner, "plan", plan)
    # Two distinct models: count is SPU-level, not the number of variants.
    products = [product(), product(9201).model_copy(update={"spu_id": 92})]
    query = adapter(make_catalog(monkeypatch, products=products))
    record = await query.execute(query.prepare(
        "g1", "catalog_search", {"query": "给我一个键盘候选", "limit": 1},
    ))
    output = record.result.output
    assert output["status"] == "success"
    assert output["total_match_count"] == 2
    assert output["returned_count"] == 1
    assert output["is_exhaustive"] is False
    with pytest.raises(EvidenceError, match="incomplete_evidence"):
        query.evidence.reference("g1", record.evidence_id, require_complete=True)
    assert len(query.evidence.presentation("g1", record.evidence_id)["candidates"]["products"]) == 1


@pytest.mark.parametrize("query_text", ["要白色蓝牙键盘", "键盘必须白色，而且需要蓝牙"])
async def test_same_sku_constraints_and_model_variant_scope_survive_adapter(
    monkeypatch, query_text,
):
    from agent_loop.support.domain_tools import FixtureSearchPlanner
    from app.schemas.catalog_search import SearchCondition, SearchPlan

    requests = []

    async def plan(self, request):
        requests.append(request)
        # Fixed semantic plan tests adapter + domain rules, not real LLM interpretation.
        return SearchPlan(category="键盘", category_evidence="键盘", conditions=[
            SearchCondition(key="color", values=["白色"], evidence="白色"),
            SearchCondition(key="connection_type", values=["蓝牙"], evidence="蓝牙"),
        ])

    monkeypatch.setattr(FixtureSearchPlanner, "plan", plan)
    catalog = make_catalog(monkeypatch)
    query = adapter(catalog)
    arguments = {"query": query_text}
    record = await query.execute(query.prepare("g1", "catalog_search", arguments))
    direct = await execute_bound_tool(
        catalog.get_by_llm_name("catalog_search"), arguments, {"user_id": 41},
    )
    assert record.result == direct
    assert all(request.query == query_text for request in requests)
    output = record.result.output
    assert output["selection_scope"] == "spu"
    assert output["total_match_count"] == 1
    assert output["products"][0]["candidate_sku_ids"] == [9101]
    assert output["products"][0]["price"] == "299"
    assert output["products"][0]["match_kind"] == "exact"
    # The cheaper black/USB variant must not contribute its price or specifications.
    assert 9102 not in output["products"][0]["candidate_sku_ids"]


@pytest.mark.parametrize("configured", [True, False])
async def test_missing_web_material_and_missing_configuration_remain_distinct(configured):
    import httpx
    from app.tools.web_search import WebSearchToolService

    service = WebSearchToolService(
        "fixture" if configured else "",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []})),
    )
    contract = next(c for c in default_tool_contracts() if c.llm_name == "knowledge_search")
    query = adapter(ToolCatalog([BoundTool(contract, service.search)]))
    record = await query.execute(query.prepare("g1", "knowledge_search", {"query": "接口文档"}))
    if configured:
        assert record.result.ok
        assert record.result.output["result_type"] == "empty"
        assert "no_retrieval_evidence" in record.completion_blockers
    else:
        assert not record.result.ok
        assert record.result.error.code == "dependency_unavailable"
        assert record.result.error.next_action == "stop"
        assert record.result.output is None
    with pytest.raises(EvidenceError, match="incomplete_evidence"):
        query.evidence.reference("g1", record.evidence_id, require_complete=True)
