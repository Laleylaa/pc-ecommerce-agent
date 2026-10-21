from typing import cast

import pytest
from app.core.config import Settings
from app.models import KnowledgeDocument
from app.services.knowledge_rag import ChromaKnowledgeService, LocalHashEmbeddingProvider
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.knowledge_knowledge_rag import (
    FakeKnowledgeRepository,
    InMemoryChromaCollection,
)


@pytest.mark.asyncio
async def test_knowledge_service_syncs_documents_to_chroma_and_returns_evidence() -> None:
    documents = [
        KnowledgeDocument(
            id=1,
            title="七天无理由退货政策",
            document_type="policy",
            content="自签收次日起七天内，商品未影响二次销售，可申请七天无理由退货。",
            metadata_json={"scenario": "return"},
        ),
        KnowledgeDocument(
            id=2,
            title="外设保修说明",
            document_type="policy",
            content="鼠标、键盘、耳机等外设通常享受一年有限保修。",
            metadata_json={"scenario": "warranty"},
        ),
    ]
    collection = InMemoryChromaCollection()
    service = ChromaKnowledgeService(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        collection=collection,
    )
    service.repository = FakeKnowledgeRepository(documents)  # type: ignore[assignment]

    evidence = await service.retrieve("退货政策怎么走")

    assert set(collection.records) == {"knowledge_document:1", "knowledge_document:2"}
    assert documents[0].chroma_collection == "pc_agent_knowledge"
    assert documents[0].chroma_id == "knowledge_document:1"
    assert evidence[0].source_id == 1
    assert evidence[0].title == "七天无理由退货政策"
    assert "退货" in evidence[0].snippet
    assert evidence[0].score and evidence[0].score > 0.16


@pytest.mark.asyncio
async def test_knowledge_service_filters_low_similarity_results() -> None:
    documents = [
        KnowledgeDocument(
            id=1,
            title="七天无理由退货政策",
            document_type="policy",
            content="自签收次日起七天内，商品未影响二次销售，可申请七天无理由退货。",
            metadata_json={"scenario": "return"},
        )
    ]
    service = ChromaKnowledgeService(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        collection=InMemoryChromaCollection(),
    )
    service.repository = FakeKnowledgeRepository(documents)  # type: ignore[assignment]

    evidence = await service.retrieve("帮我查最近订单")

    assert evidence == []


def test_local_hash_embedding_is_deterministic() -> None:
    provider = LocalHashEmbeddingProvider(dimensions=32)

    assert provider.embed_query("退货政策") == provider.embed_query("退货政策")
