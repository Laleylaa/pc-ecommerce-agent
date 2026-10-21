"""P3 order conditions and contradictory retrieval material retain their domain scope."""

import json
from datetime import datetime

import pytest
from agent_loop.support.domain_tools import make_catalog
from agent_loop.support.loop_runtime import calls, final_answer, goal, register, runtime_fixture
from app.repositories.orders import OrderRepository
from app.schemas.chat import ChatRequest
from app.schemas.order import OrderFilters
from app.tools.contracts import BoundTool, ToolCatalog
from app.tools.knowledge import LocalKnowledgeDocument
from app.tools.orders import OrderQueryPlan, OrderToolService
from app.tools.schemas import OrderLookupInput

pytestmark = [pytest.mark.business, pytest.mark.asyncio]


@pytest.mark.parametrize("conflicting", [False, True])
async def test_order_time_status_filters_reach_real_repository_without_being_dropped(conflicting):
    start, end = datetime(2026, 10, 1), datetime(2026, 11, 1)
    statements = []

    class Session:
        async def execute(self, statement):
            statements.append(statement)

            class Result:
                def scalar_one(self):
                    return 2

            return Result()

    class Planner:
        async def plan(self, request):
            return OrderQueryPlan(
                query=request.query,
                selection="all",
                operation="total_count",
                requested_fields=["order_count"],
                filters=OrderFilters(
                    start_at=end if conflicting else start, end_at=end, statuses=[3],
                ),
                filter_evidence=["10月", "已发货"],
            )

    service = OrderToolService(Session(), planner=Planner())
    result = await service.lookup(OrderLookupInput(user_id=41, query="数一下10月已发货的订单"))
    if conflicting:
        assert result.result_type == "invalid_query"
        assert statements == []  # Never widen a conflicting filter to all orders.
    else:
        assert result.result_type == "order_count" and result.total_match_count == 2
        params = statements[0].compile().params
        assert params["user_id_1"] == 41 and params["status_1"] == [3]
        assert start in params.values() and end in params.values()


async def test_missing_logistics_does_not_become_promised_arrival(monkeypatch):
    statements = []

    class Planner:
        async def plan(self, request):
            return OrderQueryPlan(
                selection="latest", operation="detail", requested_fields=["estimated_arrival"],
            )

    class Repository:
        def __init__(self, session):
            statements.append("unexpected query")

    monkeypatch.setattr("app.tools.orders.OrderRepository", Repository)
    result = await OrderToolService(None, planner=Planner()).lookup(
        OrderLookupInput(user_id=41, query="最后一笔预计哪天送到"),
    )
    assert result.result_type == "unsupported"
    assert result.order is None and statements == []


async def test_conflicting_policy_material_keeps_both_sources_and_external_scope(monkeypatch):
    message = "核对退货配件要求，再查外部接口资料"
    runtime, provider, _ = runtime_fixture(
        monkeypatch,
        [
            register(goal(message)),
            calls(("policy_search", {"query": "退货配件要求"})),
            calls(("knowledge_search", {"query": "键盘连接接口"})),
            {"content": "stop without declaring conflicting evidence complete"},
            final_answer(),
        ],
    )
    catalog = make_catalog(monkeypatch)
    policy = catalog.get_by_llm_name("policy_search").handler.__self__
    policy._documents = [
        LocalKnowledgeDocument(
            id=identity, title=f"退货配件规则{identity}", document_type="policy", content=content,
        )
        for identity, content in [
            (7301, "退货必须包含全部配件。"), (7302, "退货不需要任何配件。"),
        ]
    ]
    runtime.catalog_factory = lambda model: ToolCatalog(
        [BoundTool(b.contract, b.handler) for b in catalog.list_bound_tools()]
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    policy_docs = facts["evidence"][0]["result"]["output"]["documents"]
    assert {d["source_id"] for d in policy_docs} == {7301, 7302}
    assert {d["snippet"] for d in policy_docs} == {"退货必须包含全部配件。", "退货不需要任何配件。"}
    assert "retrieval_not_exhaustive" in facts["evidence"][0]["completion_blockers"]
    assert response.evidence[-1].source_type == "web_page"
    assert all(d["source_type"] != "web_page" for d in policy_docs)


async def test_order_repository_logistics_query_keeps_owner_even_without_results():
    statements = []

    class Session:
        async def execute(self, statement):
            statements.append(statement)

            class Empty:
                def scalar_one_or_none(self):
                    return None

            return Empty()

    result = await OrderRepository(Session()).latest_order(42, filters=OrderFilters(statuses=[3]))
    assert result is None
    params = statements[0].compile().params
    assert params["user_id_1"] == 42 and params["status_1"] == [3]
