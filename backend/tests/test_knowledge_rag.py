from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.agent.intent import classify_boundary
from app.core.config import Settings
from app.models import KnowledgeDocument
from app.schemas.chat import EvidenceItem
from app.services.knowledge_rag import ChromaKnowledgeService, LocalHashEmbeddingProvider


class FakeKnowledgeRepository:
    def __init__(self, documents: list[KnowledgeDocument]):
        self.documents = documents

    async def list_documents(self, limit: int = 500) -> list[KnowledgeDocument]:
        return self.documents[:limit]

    async def list_documents_by_ids(self, document_ids: list[int]) -> list[KnowledgeDocument]:
        by_id = {document.id: document for document in self.documents}
        return [by_id[document_id] for document_id in document_ids if document_id in by_id]

    async def mark_indexed(
        self, document: KnowledgeDocument, collection_name: str, chroma_id: str
    ) -> None:
        document.chroma_collection = collection_name
        document.chroma_id = chroma_id


class InMemoryChromaCollection:
    def __init__(self):
        self.records: dict[str, dict[str, Any]] = {}

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        for index, chroma_id in enumerate(ids):
            self.records[chroma_id] = {
                "document": documents[index],
                "metadata": metadatas[index],
                "embedding": embeddings[index],
            }

    def query(
        self,
        query_embeddings: list[list[float]],
        n_results: int,
        include: list[str],
    ) -> dict[str, list[list[Any]]]:
        query_embedding = query_embeddings[0]
        ranked = sorted(
            self.records.items(),
            key=lambda item: _cosine_similarity(query_embedding, item[1]["embedding"]),
            reverse=True,
        )[:n_results]
        distances = [
            1.0 - _cosine_similarity(query_embedding, record["embedding"])
            for _, record in ranked
        ]
        return {
            "ids": [[chroma_id for chroma_id, _ in ranked]],
            "documents": [[record["document"] for _, record in ranked]],
            "metadatas": [[record["metadata"] for _, record in ranked]],
            "distances": [distances],
        }


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


@pytest.mark.asyncio
async def test_after_sales_fallback_answer_includes_knowledge_evidence() -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    evidence = EvidenceItem(
        source_type="knowledge_document",
        source_id=1,
        title="七天无理由退货政策",
        document_type="policy",
        snippet="自签收次日起七天内，商品未影响二次销售，可申请七天无理由退货。",
        score=0.42,
        metadata={"scenario": "return"},
    )
    state = {
        "message": "退货政策怎么走",
        "intent": "after_sales",
        "boundary": classify_boundary("退货政策怎么走").model_dump(mode="json"),
        "decision": {
            "type": "grounded_response",
            "response": "",
            "reason": "test",
            "tool_calls": [],
        },
        "parsed": {},
        "evidence": [evidence],
    }

    result = await runtime._finalize_response(state)

    assert "知识库" in result["answer"]
    assert "七天无理由退货政策" in result["answer"]
    assert result["suggested_actions"] == []


@pytest.mark.asyncio
async def test_multi_document_fallback_does_not_dump_raw_snippets() -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    evidence = [
        EvidenceItem(
            source_type="knowledge_document",
            source_id=index,
            title=title,
            document_type="faq",
            snippet=f"raw snippet {index}",
            score=0.4,
            metadata={},
        )
        for index, title in enumerate(("商家说明", "订单规则", "常见问题"), start=1)
    ]
    state = {
        "message": "你们商店的理念是什么",
        "intent": "knowledge_search",
        "boundary": classify_boundary("你们商店的理念是什么").model_dump(mode="json"),
        "decision": {
            "type": "grounded_response",
            "response": "",
            "reason": "test",
            "tool_calls": [],
        },
        "parsed": {},
        "evidence": evidence,
    }

    result = await runtime._finalize_response(state)

    assert "暂时无法可靠归纳出直接答案" in result["answer"]
    assert "raw snippet" not in result["answer"]


def test_local_hash_embedding_is_deterministic() -> None:
    provider = LocalHashEmbeddingProvider(dimensions=32)

    assert provider.embed_query("退货政策") == provider.embed_query("退货政策")


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(left_item * right_item for left_item, right_item in zip(left, right, strict=True))
