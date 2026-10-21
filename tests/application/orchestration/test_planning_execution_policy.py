import json
from types import SimpleNamespace

import pytest
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from langchain_core.messages import AIMessage

from tests.support.tool_plan_fixtures import order_plan


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["knowledge_search", "policy_search"])
async def test_real_document_tool_returns_at_most_requested_evidence(monkeypatch, tool):
    import httpx
    from app.tools import knowledge
    from app.tools.web_search import WebSearchToolService

    monkeypatch.setattr(knowledge, "_load_vector_index", lambda *args: None)
    service = knowledge.KnowledgeRetrievalToolService(
        documents=[
            knowledge.LocalKnowledgeDocument(
                id=i,
                title=f"测试材料{i}",
                document_type="policy",
                content="查询测试内容",
            )
            for i in range(5)
        ],
        embedding_provider=SimpleNamespace(model_name="fixture"),
    )
    web = WebSearchToolService("fixture", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": [
            {"title": f"材料{i}", "url": f"https://example.com/{i}",
             "content": "事实", "score": 1}
            for i in range(5)
        ]}),
    ))
    handler = service.search_policy if tool == "policy_search" else web.search
    contract = DefaultToolContractProvider().get_contract(tool)
    for arguments, expected in [
        ({"query": "查询"}, 3),
        ({"query": "查询", "limit": 1}, 1),
        ({"query": "查询", "limit": 4}, 4),
    ]:
        result = await execute_bound_tool(BoundTool(contract, handler), arguments, {"user_id": 7})
        assert result.ok and len(result.output["documents"]) == expected


@pytest.mark.asyncio
async def test_real_order_tool_uses_caller_limit_instead_of_reparsing_query(monkeypatch):
    from app.repositories.orders import OrderSearchPage
    from app.tools import orders

    calls = []

    class Repository:
        def __init__(self, session):
            pass

        async def list_recent_orders_page(self, user_id, *, limit, offset):
            calls.append((user_id, limit, offset))
            return OrderSearchPage(orders=[], total_count=0, offset=offset)

    monkeypatch.setattr(orders, "OrderRepository", Repository)
    class Planner:
        async def plan(self, request):
            return orders.OrderQueryPlan(selection="recent", operation="list",
                                         requested_fields=["order_summary"])

    service = orders.OrderToolService(None, planner=Planner())
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    for args, expected in [
        ({"query": "最近两笔订单"}, 5),
        ({"query": "最近五笔订单", "limit": 2}, 2),
    ]:
        result = await execute_bound_tool(BoundTool(contract, service.lookup), args, {"user_id": 7})
        assert result.ok and calls[-1] == (7, expected, 0)


@pytest.mark.asyncio
async def test_order_internal_llm_cannot_override_caller_quantity():
    from app.tools.orders import LLMOrderQueryPlanner
    from app.tools.schemas import OrderLookupInput

    from tests.support.tool_planning import scripted_review

    class Model:
        async def ainvoke(self, messages):
            if assessment := scripted_review(messages):
                return assessment
            return AIMessage(
                content=json.dumps(
                    order_plan({
                        "selection": "recent", "operation": "list",
                        "requested_fields": ["order_summary"], "limit": 5,
                    })
                )
            )

    result = await LLMOrderQueryPlanner(Model()).plan(
        OrderLookupInput(
            user_id=7,
            query="最近两笔订单",
            limit=2,
        )
    )
    assert result.limit == 2
