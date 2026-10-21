from typing import Any

from app.models import KnowledgeDocument


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


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(left_item * right_item for left_item, right_item in zip(left, right, strict=True))
