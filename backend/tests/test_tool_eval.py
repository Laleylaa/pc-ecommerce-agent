
import pytest

from app.tools.catalog import RuleBasedCatalogQueryPlanner, validate_catalog_compare_plan
from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    LocalKnowledgeDocument,
)
from app.tools.orders import _extract_order_id
from app.tools.schemas import CatalogCompareInput, DocumentSearchInput


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("查一下订单 202607020001", 202607020001),
        ("我的订单号是202607020001，谢谢", 202607020001),
        ("预算 300，订单 202607020001", 202607020001),
        ("预算300以内", None),
        ("订单 202607020001 和 202607020002 都看看", None),
        ("短号 1234567", None),
    ],
)
def test_order_lookup_query_order_id_eval_cases(query: str, expected: int | None) -> None:
    assert _extract_order_id(query) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_fields"),
    [
        ("Compare two FPS mice by DPI and weight", {"max_dpi", "weight_g"}),
        (
            "Compare wireless and wired headset microphone",
            {"connection_type", "wireless", "microphone"},
        ),
        ("Compare 144Hz 2K monitors", {"refresh_rate", "resolution"}),
        ("Which one has better sales", {"sku_sales_count", "sales_count"}),
    ],
)
async def test_catalog_compare_planner_field_eval_cases(
    query: str, expected_fields: set[str]
) -> None:
    raw_plan = await RuleBasedCatalogQueryPlanner().plan_compare(
        CatalogCompareInput(query=query, limit=5)
    )
    plan = validate_catalog_compare_plan(raw_plan)

    assert expected_fields <= set(plan.comparison_fields)


def _document_service() -> KnowledgeRetrievalToolService:
    documents = [
        LocalKnowledgeDocument(
            id=1,
            title="Return and refund policy",
            document_type="policy",
            content="退货 退款 return refund warranty exchange 七天无理由 售后政策",
            metadata={"scenario": "after_sales"},
        ),
        LocalKnowledgeDocument(
            id=2,
            title="Logitech brand guide",
            document_type="brand",
            content="Logitech 罗技 鼠标 键盘 外设 品牌 特点 无线 办公 游戏",
            metadata={"scenario": "brand"},
        ),
        LocalKnowledgeDocument(
            id=3,
            title="Shopping FAQ",
            document_type="faq",
            content="订单 查询 发票 发货 库存 常见问题 FAQ",
            metadata={"scenario": "faq"},
        ),
    ]
    vector_index = KnowledgeVectorIndex(
        version=1,
        embedding_provider="sentence_transformers",
        embedding_model="different-test-model",
        documents_hash="test",
        chunk_size=420,
        chunk_overlap=80,
        query_instruction="",
        chunks=[],
    )
    return KnowledgeRetrievalToolService(documents=documents, vector_index=vector_index)


@pytest.mark.asyncio
async def test_policy_search_eval_prefers_policy_and_faq_types() -> None:
    result = await _document_service().search_policy(
        DocumentSearchInput(query="退货退款保修政策", retrieval_mode="bm25", limit=5)
    )

    assert result.result_type == "documents"
    assert result.documents[0].document_type == "policy"
    assert {item.document_type for item in result.documents} <= {"policy", "store_rule", "faq"}


@pytest.mark.asyncio
async def test_policy_search_eval_rejects_brand_document_type_filter() -> None:
    result = await _document_service().search_policy(
        DocumentSearchInput(
            query="Logitech 品牌", document_type="brand", retrieval_mode="bm25", limit=5
        )
    )

    assert result.result_type == "empty"
    assert result.documents == []


@pytest.mark.asyncio
async def test_knowledge_search_eval_can_return_brand_docs() -> None:
    result = await _document_service().search_knowledge(
        DocumentSearchInput(query="Logitech 罗技 品牌", retrieval_mode="bm25", limit=5)
    )

    assert result.result_type == "documents"
    assert result.documents[0].document_type == "brand"
    assert result.documents[0].source_id == 2


@pytest.mark.asyncio
async def test_knowledge_search_eval_rejects_policy_document_type_filter() -> None:
    result = await _document_service().search_knowledge(
        DocumentSearchInput(
            query="退货政策", document_type="policy", retrieval_mode="bm25", limit=5
        )
    )

    assert result.result_type == "empty"
    assert result.documents == []
