from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    LocalKnowledgeDocument,
)


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
