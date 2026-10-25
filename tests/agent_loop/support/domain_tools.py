"""Independent P1 fixtures: real five services, synthetic data and HTTP only."""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
from app.agent.loop.tool_adapter import AdmissionError, QueryToolAdapter
from app.repositories.catalog_search import CatalogRecord, CatalogSearchRepository
from app.schemas.catalog import ProductCard
from app.schemas.order import OrderCard
from app.tools.catalog.facets import CatalogFacetService
from app.tools.catalog.planner import explicit_lookup
from app.tools.catalog.service import CatalogSearchService
from app.tools.contracts import (
    BoundTool,
    ToolCatalog,
    ToolRuntimeContext,
    default_tool_contracts,
)
from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    LocalKnowledgeDocument,
)
from app.tools.orders import OrderToolService
from app.tools.web_search import WebSearchToolService

TOOL_ARGUMENTS = {
    "catalog_search": {"query": "SKU 9101"},
    "catalog_facets": {"query": "键盘有哪些规格"},
    "order_lookup": {"order_id": 8201},
    "policy_search": {"query": "退货配件要求"},
    "knowledge_search": {"query": "键盘连接接口"},
}


def admit_goal(goal_id):
    if goal_id not in {"g1", "g2"}:
        raise AdmissionError("goal_not_admitted")


class NoDatabase:
    def __getattr__(self, name):
        raise AssertionError(f"P1 fixture forbids database access: {name}")


class NoEmbedding:
    model_name = "p1-no-embedding"

    def embed_query(self, text):
        raise AssertionError("P1 fixture forbids model downloads")


class FixtureSearchPlanner:
    """Fixed explicit-ID plan for adapter tests, not a natural-language interpreter."""

    async def plan(self, request):
        plan = explicit_lookup(request.query)
        if plan is None:
            raise AssertionError("This fixture needs an explicitly scripted search plan")
        plan.operation = "search"
        plan.purpose = "search"
        return plan


def product(sku=9101, *, price=299, color="白色", connection="蓝牙"):
    return ProductCard(
        sku_id=sku, spu_id=91, title=f"P1 测试键盘 {sku}", spu_title="P1 测试键盘",
        brand="P1合成品牌", category="键盘", price=Decimal(price), stock=4,
        specs={"color": color, "connection_type": connection},
    )


def make_catalog(monkeypatch, *, products=None):
    if products is None:
        products = [product(), product(9102, price=199, color="黑色", connection="USB")]

    async def load(repository, category, scope):
        assert isinstance(repository.session, NoDatabase)
        return [CatalogRecord(product=p.model_copy(deep=True)) for p in products if (
            (not category or category == p.category)
            and (not scope.sku_ids or p.sku_id in scope.sku_ids)
            and (not scope.spu_ids or p.spu_id in scope.spu_ids)
        )]

    monkeypatch.setattr(CatalogSearchRepository, "load", load)

    class OrderRows:
        def __init__(self, session):
            assert isinstance(session, NoDatabase)

        async def get_order(self, user_id, order_id, **kwargs):
            # Synthetic ownership mapping, independent of adapter identity injection.
            if (user_id, order_id) != (41, 8201):
                return None
            return OrderCard(
                id=8201, status=3, status_label="已发货", pay_amount=299,
                created_at=datetime(2026, 10, 1, tzinfo=UTC), items=[], logistics=None,
            )

    monkeypatch.setattr("app.tools.orders.OrderRepository", OrderRows)
    policy = KnowledgeRetrievalToolService(
        documents=[LocalKnowledgeDocument(
            id=7301, title="合成退货配件要求", document_type="policy",
            content="合成政策：退货须保留全部配件，审核通过后处理。",
        )],
        vector_index=KnowledgeVectorIndex(
            version=1, embedding_provider="sentence_transformers", embedding_model="fixture",
            documents_hash="fixture", chunk_size=500, chunk_overlap=0,
            query_instruction="", chunks=[],
        ), embedding_provider=NoEmbedding(),
    )
    web = WebSearchToolService("synthetic", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"results": [{
            "title": "合成接口文档", "url": "https://example.test/p1/interfaces",
            "content": "合成资料：此版本有 USB 接口。", "score": 0.9,
        }]}),
    ))
    session = NoDatabase()
    handlers = {
        "catalog_search": CatalogSearchService(
            CatalogSearchRepository(session), planner=FixtureSearchPlanner(),
        ).search,
        "catalog_lookup": CatalogSearchService(
            CatalogSearchRepository(session), operation="lookup",
        ).search,
        "catalog_facets": CatalogFacetService(session).facets,
        "order_lookup": OrderToolService(session).lookup,
        "policy_search": policy.search_policy,
        "knowledge_search": web.search,
    }
    return ToolCatalog([BoundTool(c, handlers[c.llm_name]) for c in default_tool_contracts()])


def adapter(catalog, **kwargs):
    return QueryToolAdapter(
        catalog, ToolRuntimeContext(user_id=41), admit_goal=admit_goal, **kwargs,
    )
