from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import KnowledgeDocument
from app.repositories.knowledge import KnowledgeRepository
from app.schemas.chat import EvidenceItem

DEFAULT_COLLECTION_NAME = "pc_agent_knowledge"
DEFAULT_EMBEDDING_DIMENSIONS = 256
DEFAULT_SCORE_THRESHOLD = 0.16


class EmbeddingProvider(Protocol):
    def embed_query(self, text: str) -> list[float]:
        ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class LocalHashEmbeddingProvider:
    """Deterministic local embeddings for offline tests and keyless local demos."""

    def __init__(self, dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS):
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign * (1.0 + min(len(token), 4) * 0.1)

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [round(value / norm, 6) for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


class ChromaKnowledgeService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        embedding_provider: EmbeddingProvider | None = None,
        collection: Any | None = None,
    ):
        self.repository = KnowledgeRepository(session)
        self.settings = settings
        self.embedding_provider = embedding_provider or LocalHashEmbeddingProvider()
        self.collection = collection

    async def retrieve(self, query: str, limit: int = 3) -> list[EvidenceItem]:
        documents = await self.repository.list_documents()
        if not documents:
            return []

        collection = await self._get_collection()
        await self._sync_documents(collection, documents)
        raw_results = await asyncio.to_thread(
            collection.query,
            query_embeddings=[self.embedding_provider.embed_query(query)],
            n_results=max(1, min(limit, len(documents))),
            include=["documents", "metadatas", "distances"],
        )
        return await self._to_evidence(raw_results, limit)

    async def sync(self) -> int:
        documents = await self.repository.list_documents()
        if not documents:
            return 0

        collection = await self._get_collection()
        await self._sync_documents(collection, documents)
        return len(documents)

    async def _get_collection(self) -> Any:
        if self.collection is not None:
            return self.collection

        def get_collection() -> Any:
            import chromadb

            client = chromadb.HttpClient(
                host=self.settings.chroma_host,
                port=self.settings.chroma_port,
            )
            return client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

        self.collection = await asyncio.to_thread(get_collection)
        return self.collection

    @property
    def collection_name(self) -> str:
        return getattr(self.settings, "knowledge_collection", DEFAULT_COLLECTION_NAME)

    @property
    def score_threshold(self) -> float:
        return getattr(self.settings, "knowledge_score_threshold", DEFAULT_SCORE_THRESHOLD)

    async def _sync_documents(
        self, collection: Any, documents: Sequence[KnowledgeDocument]
    ) -> None:
        ids = [_chroma_id(document.id) for document in documents]
        texts = [_document_embedding_text(document) for document in documents]
        metadatas = [_chroma_metadata(document) for document in documents]
        embeddings = self.embedding_provider.embed_documents(texts)

        await asyncio.to_thread(
            collection.upsert,
            ids=ids,
            documents=texts,
            metadatas=metadatas,
            embeddings=embeddings,
        )

        for document, chroma_id in zip(documents, ids, strict=True):
            if (
                document.chroma_collection != self.collection_name
                or document.chroma_id != chroma_id
            ):
                await self.repository.mark_indexed(document, self.collection_name, chroma_id)

    async def _to_evidence(self, raw_results: dict[str, Any], limit: int) -> list[EvidenceItem]:
        ids = _first_result_list(raw_results.get("ids"))
        distances = _first_result_list(raw_results.get("distances"))
        metadatas = _first_result_list(raw_results.get("metadatas"))
        document_ids = [
            int(metadata["source_id"])
            for metadata in metadatas
            if isinstance(metadata, dict) and metadata.get("source_id") is not None
        ]
        documents = await self.repository.list_documents_by_ids(document_ids)
        by_id = {document.id: document for document in documents}

        evidence: list[EvidenceItem] = []
        for index, _raw_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) else {}
            if not isinstance(metadata, dict) or metadata.get("source_id") is None:
                continue

            document_id = int(metadata["source_id"])
            document = by_id.get(document_id)
            if document is None:
                continue

            score = _score_from_distance(distances[index] if index < len(distances) else None)
            if score is not None and score < self.score_threshold:
                continue

            evidence.append(
                EvidenceItem(
                    source_type="knowledge_document",
                    source_id=document.id,
                    title=document.title,
                    document_type=document.document_type,
                    snippet=_snippet(document.content),
                    score=score,
                    metadata=document.metadata_json or {},
                )
            )
            if len(evidence) >= limit:
                break

        return evidence


def _tokenize(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.lower())
    tokens = re.findall(r"[a-z0-9]+", normalized)
    cjk_chars = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    tokens.extend(cjk_chars)
    for width in (2, 3):
        tokens.extend(
            "".join(cjk_chars[index : index + width])
            for index in range(max(len(cjk_chars) - width + 1, 0))
        )
    return [token for token in tokens if token.strip()]


def _document_embedding_text(document: KnowledgeDocument) -> str:
    metadata = document.metadata_json or {}
    metadata_text = " ".join(str(value) for value in metadata.values())
    return "\n".join(
        [
            document.title,
            document.title,
            document.document_type,
            metadata_text,
            document.content,
        ]
    )


def _chroma_metadata(document: KnowledgeDocument) -> dict[str, str | int | float | bool]:
    metadata: dict[str, str | int | float | bool] = {
        "source_type": "knowledge_document",
        "source_id": int(document.id),
        "title": document.title,
        "document_type": document.document_type,
    }
    for key, value in (document.metadata_json or {}).items():
        metadata[f"meta_{key}"] = _metadata_scalar(value)
    return metadata


def _metadata_scalar(value: Any) -> str | int | float | bool:
    if isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _chroma_id(document_id: int) -> str:
    return f"knowledge_document:{document_id}"


def _first_result_list(value: Any) -> list[Any]:
    if not value:
        return []
    first = value[0] if isinstance(value, list) and value else value
    return first if isinstance(first, list) else []


def _score_from_distance(distance: Any) -> float | None:
    if distance is None:
        return None
    try:
        return round(max(0.0, 1.0 - float(distance)), 4)
    except (TypeError, ValueError):
        return None


def _snippet(content: str, max_length: int = 180) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 1]}..."
