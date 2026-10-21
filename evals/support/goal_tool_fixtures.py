"""Synthetic data adapters for live-model tests; never opens configured databases.

Only repository/data-loading boundaries are replaced. Tool planners and services are real.
All names/prices/orders/policies here are test fixtures, not real store facts.
"""

from datetime import datetime
from unittest.mock import patch

import httpx
from app.repositories.catalog_search import CatalogRecord
from app.repositories.orders import OrderSearchPage
from app.schemas.catalog import ProductCard
from app.schemas.order import LogisticsCard, OrderCard, OrderItemCard
from app.tools.catalog_search.service import within_scope
from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    LocalKnowledgeDocument,
)
from app.tools.web_search import WebSearchToolService


def web_search_service() -> WebSearchToolService:
    """Formal HTTP/parser code over an explicitly synthetic provider response."""
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{
            "title": "测试外设资料", "url": "https://example.com/peripheral-fixture",
            "content": "测试资料：机械轴和磁轴采用不同的触发方式。", "score": 0.9,
        }]})

    return WebSearchToolService("fixture-key", transport=httpx.MockTransport(respond))

PRODUCTS = [
    ProductCard(
        sku_id=sku,
        spu_id=spu,
        title=f"{name} {color} {layout}",
        spu_title=name,
        brand=brand,
        category="键盘",
        price=price,
        stock=stock,
        specs={
            "color": color,
            "style": layout,
            **({"connection_type": connection} if connection else {}),
        },
    )
    for sku, spu, name, brand, color, connection, layout, price, stock in [
        (101, 11, "Keychron K8", "Keychron", "白色", "蓝牙", "80%", 260, 8),
        (102, 11, "Keychron K8", "Keychron", "黑色", "USB", "80%", 230, 4),
        (201, 22, "罗技 K380", "罗技", "白色", "蓝牙", "75%", 199, 12),
        (202, 22, "罗技 K380", "罗技", "粉色", "蓝牙", "75%", 219, 5),
        (301, 33, "测试办公键盘", "测试", "白色", "蓝牙", "全尺寸", 299, 6),
        (401, 44, "测试高价键盘", "测试", "白色", "蓝牙", "75%", 599, 3),
        (501, 55, "测试缺资料键盘", "测试", "白色", "", "75%", 180, 3),
    ]
]


class DenySession:
    def __getattr__(self, name):
        raise AssertionError(f"Database I/O forbidden in fixture run: {name}")


class SearchRecords:
    async def load(self, category, scope):
        return [
            CatalogRecord(product=p.model_copy(deep=True), active=True)
            for p in PRODUCTS
            if (not category or category == p.category)
            and within_scope(CatalogRecord(product=p, active=True), scope)
        ]


async def load_catalog_records(repository, category, scope):
    assert isinstance(repository.session, DenySession)
    return await SearchRecords().load(category, scope)


ORDERS = [
    OrderCard(
        id=7001,
        status=3,
        status_label="已发货",
        pay_amount=199,
        total_amount=199,
        created_at=datetime(2026, 9, 28, 12),
        items=[
            OrderItemCard(
                id=1,
                sku_id=201,
                sku_name="测试订单中的K380",
                sku_specs={"color": "白色"},
                price=199,
                quantity=1,
            )
        ],
        logistics=LogisticsCard(
            express_company="测试物流",
            logistic_no="TEST-ONLY-001",
            status=2,
            trace=[{"time": "2026-09-29 12:00", "description": "测试包裹运输中"}],
        ),
    )
]


class OrderRows:
    def __init__(self, session):
        assert isinstance(session, DenySession)

    async def count_orders(self, user_id):
        assert user_id == 1
        return len(ORDERS)

    async def get_order(self, user_id, order_id):
        assert user_id == 1
        return next((o for o in ORDERS if o.id == order_id), None)

    async def latest_order(self, user_id):
        assert user_id == 1
        return ORDERS[0]

    async def list_recent_orders_page(self, user_id, *, limit=5, offset=0):
        assert user_id == 1
        return OrderSearchPage(
            orders=ORDERS[offset : offset + limit], total_count=len(ORDERS), offset=offset
        )


DOCUMENTS = [
    LocalKnowledgeDocument(
        id=8001,
        title="测试店铺键盘七天无理由退货政策",
        document_type="policy",
        content="仅用于评测的虚构政策：键盘拆封后，商品完好、配件齐全且不影响二次销售的，可在签收七天内申请无理由退货；拆封本身不自动排除退货。具体审核由人工处理。",
    ),
    LocalKnowledgeDocument(
        id=8002,
        title="测试知识：机械键盘红轴和青轴原理",
        document_type="peripheral_knowledge",
        content="红轴通常采用线性触发，青轴通常具有段落反馈和发声结构。具体产品表现应以该型号资料为准。",
    ),
]


def knowledge_service():
    # Empty synthetic index: formal hybrid retrieval uses its BM25 branch; no embedding downloads.
    index = KnowledgeVectorIndex(
        version=1,
        embedding_provider="sentence_transformers",
        embedding_model="fixture-empty",
        documents_hash="fixture",
        chunk_size=500,
        chunk_overlap=0,
        query_instruction="",
        chunks=[],
    )

    class NoEmbedding:
        model_name = "fixture-disabled"

        def embed_query(self, text):
            raise AssertionError("embedding I/O forbidden")

    return KnowledgeRetrievalToolService(
        documents=DOCUMENTS, vector_index=index, embedding_provider=NoEmbedding()
    )


def fixture_patches():
    return (
        patch("app.repositories.catalog_search.CatalogSearchRepository.load", load_catalog_records),
        patch("app.tools.orders.OrderRepository", OrderRows),
    )
