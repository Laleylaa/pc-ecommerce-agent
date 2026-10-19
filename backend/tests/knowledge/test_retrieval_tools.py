import pytest
from pydantic import ValidationError

import app.tools.knowledge as knowledge_tools
from app.tools.contracts import (
    DefaultToolContractProvider,
)
from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    KnowledgeVectorIndexChunk,
)
from app.tools.schemas import (
    DocumentSearchInput,
)


class FakeEmbeddingProvider:
    model_name = "fake-embedding"

    def embed_query(self, text: str) -> list[float]:
        lowered = text.lower()
        if "wooting" in lowered or "brand" in lowered:
            return [1.0, 0.0]
        if "return" in lowered or "refund" in lowered:
            return [0.0, 1.0]
        return [0.5, 0.5]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


def test_sentence_transformer_model_is_shared_across_provider_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    loads: list[tuple[str, bool]] = []

    def fake_sentence_transformer(model_name: str, *, local_files_only: bool = False):
        loads.append((model_name, local_files_only))
        return sentinel

    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        fake_sentence_transformer,
    )
    knowledge_tools._load_sentence_transformer_model.cache_clear()
    try:
        first = knowledge_tools.SentenceTransformerEmbeddingProvider("test-shared-model")
        second = knowledge_tools.SentenceTransformerEmbeddingProvider("test-shared-model")

        assert first._get_model() is sentinel
        assert second._get_model() is sentinel
        assert loads == [("test-shared-model", True)]
    finally:
        knowledge_tools._load_sentence_transformer_model.cache_clear()


def _fake_vector_index() -> KnowledgeVectorIndex:
    return KnowledgeVectorIndex(
        version=1,
        embedding_provider="sentence_transformers",
        embedding_model=FakeEmbeddingProvider.model_name,
        documents_hash="test-documents",
        chunk_size=420,
        chunk_overlap=80,
        query_instruction="",
        chunks=[
            KnowledgeVectorIndexChunk(
                document_id=1,
                chunk_id="1:0",
                text="return refund after sales",
                embedding=[0.0, 1.0],
            ),
            KnowledgeVectorIndexChunk(
                document_id=5,
                chunk_id="5:0",
                text="Logitech Razer Wooting brand",
                embedding=[1.0, 0.0],
            ),
        ],
    )


def _knowledge_service() -> KnowledgeRetrievalToolService:
    return KnowledgeRetrievalToolService(
        embedding_provider=FakeEmbeddingProvider(),
        vector_index=_fake_vector_index(),
    )


def test_document_search_public_schema_fixes_hybrid_retrieval() -> None:
    provider = DefaultToolContractProvider()

    for name in ("policy_search",):
        contract = provider.get_contract(name)
        assert contract is not None
        fields = set(contract.public_input_model.model_json_schema()["properties"])
        assert fields == {"query", "document_type", "limit"}
        with pytest.raises(ValidationError):
            contract.public_input_model.model_validate(
                {"query": "退货政策", "retrieval_mode": "bm25"}
            )
        public_input = contract.public_input_model.model_validate({"query": "退货政策"})
        internal_input = contract.internal_input_model.model_validate(
            public_input.model_dump(mode="json", exclude_none=True)
        )
        assert internal_input.retrieval_mode == "hybrid"


@pytest.mark.asyncio
async def test_policy_and_knowledge_search_support_hybrid_retrieval() -> None:
    service = _knowledge_service()
    policy = await service.search_policy(DocumentSearchInput(query="return refund", limit=3))
    knowledge = await service.search_knowledge(
        DocumentSearchInput(query="Logitech Razer brand", limit=3)
    )

    assert policy.result_type == "documents"
    assert policy.search_strategy == "hybrid"
    assert policy.documents[0].document_type in {"policy", "store_rule", "faq"}
    assert "retrieval_debug" in policy.documents[0].metadata

    assert knowledge.result_type == "documents"
    assert knowledge.search_strategy == "hybrid"
    assert knowledge.documents[0].document_type in {
        "brand",
        "peripheral_knowledge",
        "faq",
        "store_rule",
    }
    assert any(document.document_type == "brand" for document in knowledge.documents)


@pytest.mark.asyncio
async def test_document_search_limit_bounds_returned_chunks() -> None:
    documents = [
        knowledge_tools.LocalKnowledgeDocument(
            id=1,
            title="退货政策一",
            document_type="policy",
            content="退货政策支持七天无理由退货。",
        ),
        knowledge_tools.LocalKnowledgeDocument(
            id=2,
            title="退货政策二",
            document_type="faq",
            content="退货申请需要提供订单号。",
        ),
    ]
    service = KnowledgeRetrievalToolService(documents=documents)

    result = await service.search_policy(
        DocumentSearchInput(query="退货政策", retrieval_mode="bm25", limit=1)
    )

    assert len(result.documents) == 1
    assert result.documents[0].metadata["retrieval_debug"]["chunk_id"] in {"1:0", "2:0"}


@pytest.mark.asyncio
async def test_document_search_returns_complete_top_chunks_without_overlap_only_tail() -> None:
    first_section = "退货条件：签收七天内、包装完整且不影响二次销售。" * 10
    second_section = "退款时效：退货入库验收通过后按原支付路径退回。" * 10
    document = knowledge_tools.LocalKnowledgeDocument(
        id=1,
        title="PC 外设退货政策",
        document_type="policy",
        content=first_section + second_section,
    )
    service = KnowledgeRetrievalToolService(documents=[document])

    result = await service.search_policy(
        DocumentSearchInput(query="退货条件和退款时效", retrieval_mode="bm25", limit=3)
    )

    chunk_ids = [item.metadata["retrieval_debug"]["chunk_id"] for item in result.documents]
    assert len(result.documents) == 2
    assert set(chunk_ids) == {"1:0", "1:1"}
    assert max(len(item.snippet) for item in result.documents) == 420
    assert all(not item.snippet.endswith("...") for item in result.documents)


@pytest.mark.asyncio
async def test_policy_search_finds_customer_privacy_boundary() -> None:
    service = _knowledge_service()

    result = await service.search_policy(
        DocumentSearchInput(
            query="查询其他用户购买记录和客户名单的隐私规则",
            retrieval_mode="bm25",
            limit=3,
        )
    )

    assert result.result_type == "documents"
    assert result.documents[0].title == "用户隐私与数据访问规则"
    assert any("不得查询、披露、汇总或推断其他用户" in item.snippet for item in result.documents)


@pytest.mark.asyncio
async def test_document_search_can_select_retrieval_mode() -> None:
    service = _knowledge_service()
    bm25 = await service.search_knowledge(
        DocumentSearchInput(query="keyboard magnetic switch", retrieval_mode="bm25", limit=3)
    )
    vector = await service.search_knowledge(
        DocumentSearchInput(query="Wooting", retrieval_mode="vector", limit=3)
    )

    assert bm25.search_strategy == "bm25"
    assert bm25.result_type == "documents"
    assert bm25.documents[0].score > 0

    assert vector.search_strategy == "vector"
    assert vector.result_type == "documents"
    assert vector.documents[0].metadata["retrieval_debug"]["vector_score"] > 0
    assert vector.documents[0].snippet == "Logitech Razer Wooting brand"


@pytest.mark.asyncio
async def test_bm25_retrieval_does_not_load_embedding_model() -> None:
    class UnexpectedEmbeddingProvider(FakeEmbeddingProvider):
        def embed_query(self, text: str) -> list[float]:
            raise AssertionError("BM25 retrieval must not invoke the embedding provider")

    service = KnowledgeRetrievalToolService(
        embedding_provider=UnexpectedEmbeddingProvider(),
        vector_index=_fake_vector_index(),
    )

    result = await service.search_knowledge(
        DocumentSearchInput(
            query="Logitech 品牌",
            retrieval_mode="bm25",
            limit=3,
        )
    )

    assert result.result_type == "documents"
