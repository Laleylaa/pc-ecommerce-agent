import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.tools.schemas import DocumentSearchHit, DocumentSearchInput, DocumentSearchOutput

POLICY_DOCUMENT_TYPES = {"policy", "store_rule", "faq"}
KNOWLEDGE_DOCUMENT_TYPES = {"brand", "peripheral_knowledge", "faq", "store_rule"}
DEFAULT_KNOWLEDGE_DOCUMENT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "knowledge_documents.json"
)
DEFAULT_KNOWLEDGE_VECTOR_INDEX_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "knowledge_vector_index.json"
)
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
VECTOR_INDEX_VERSION = 1
RRF_K = 10
CHUNK_SIZE = 420
CHUNK_OVERLAP = 80
MIN_CHUNK_TOP_K = 2


class EmbeddingProvider(Protocol):
    model_name: str

    def embed_query(self, text: str) -> list[float]:
        ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class LocalKnowledgeDocument(BaseModel):
    id: int
    title: str
    document_type: str
    content: str
    metadata: dict = Field(default_factory=dict)

    @property
    def metadata_json(self) -> dict:
        return self.metadata


class KnowledgeVectorIndexChunk(BaseModel):
    document_id: int
    chunk_id: str
    text: str
    embedding: list[float]


class KnowledgeVectorIndex(BaseModel):
    version: int
    embedding_provider: Literal["sentence_transformers"]
    embedding_model: str
    documents_hash: str
    chunk_size: int
    chunk_overlap: int
    query_instruction: str
    chunks: list[KnowledgeVectorIndexChunk] = Field(default_factory=list)


@dataclass(frozen=True)
class RankedChunk:
    document: LocalKnowledgeDocument
    chunk_id: str
    text: str
    score: float
    bm25_score: float = 0.0
    vector_score: float = 0.0
    bm25_rank: int | None = None
    vector_rank: int | None = None


@dataclass(frozen=True)
class LocalKnowledgeChunk:
    document: LocalKnowledgeDocument
    chunk_id: str
    text: str


@lru_cache(maxsize=4)
def _load_sentence_transformer_model(model_name: str):
    from sentence_transformers import SentenceTransformer

    try:
        return SentenceTransformer(model_name, local_files_only=True)
    except OSError:
        # A fresh environment may not have the model yet. Allow one normal Hub download;
        # subsequent providers in this process reuse the cached model object.
        return SentenceTransformer(model_name)


class SentenceTransformerEmbeddingProvider:
    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        query_instruction: str = BGE_QUERY_INSTRUCTION,
    ):
        self.model_name = model_name
        self.query_instruction = query_instruction
        self._model = None

    def embed_query(self, text: str) -> list[float]:
        return self._encode([f"{self.query_instruction}{text}"])[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts)

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._get_model()
        embeddings = model.encode(
            list(texts),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in embedding] for embedding in embeddings]

    def _get_model(self):
        if self._model is None:
            self._model = _load_sentence_transformer_model(self.model_name)
        return self._model


class KnowledgeRetrievalToolService:
    def __init__(
        self,
        documents: list[LocalKnowledgeDocument] | None = None,
        document_path: Path = DEFAULT_KNOWLEDGE_DOCUMENT_PATH,
        vector_index_path: Path = DEFAULT_KNOWLEDGE_VECTOR_INDEX_PATH,
        embedding_provider: EmbeddingProvider | None = None,
        vector_index: KnowledgeVectorIndex | None = None,
    ):
        self._documents = documents
        self.document_path = document_path
        self.vector_index_path = vector_index_path
        self.embedding_provider = embedding_provider or SentenceTransformerEmbeddingProvider()
        self._vector_index = vector_index

    async def search_policy(self, request: DocumentSearchInput) -> DocumentSearchOutput:
        return await self._search(request, POLICY_DOCUMENT_TYPES)

    async def search_knowledge(self, request: DocumentSearchInput) -> DocumentSearchOutput:
        return await self._search(request, KNOWLEDGE_DOCUMENT_TYPES)

    async def _search(
        self, request: DocumentSearchInput, default_document_types: set[str]
    ) -> DocumentSearchOutput:
        documents = self._documents or _load_local_documents(self.document_path)
        allowed_types = (
            {request.document_type} & default_document_types
            if request.document_type
            else default_document_types
        )
        candidates = [
            document for document in documents if document.document_type in allowed_types
        ]
        vector_index = self._vector_index or _load_vector_index(
            self.vector_index_path,
            self.document_path,
        )
        effective_limit = max(MIN_CHUNK_TOP_K, request.limit)
        hits = _rank_chunks(
            request.query,
            candidates,
            request.retrieval_mode,
            self.embedding_provider,
            vector_index,
        )[:effective_limit]
        return DocumentSearchOutput(
            result_type="documents" if hits else "empty",
            documents=hits,
            search_strategy=request.retrieval_mode,
        )


def _rank_chunks(
    query: str,
    documents: list[LocalKnowledgeDocument],
    retrieval_mode: str,
    embedding_provider: EmbeddingProvider,
    vector_index: KnowledgeVectorIndex | None,
) -> list[DocumentSearchHit]:
    if not documents:
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    chunks = [chunk for document in documents for chunk in _document_chunks(document)]
    bm25_ranked = (
        _rank_by_bm25(query_tokens, chunks)
        if retrieval_mode in {"bm25", "hybrid"}
        else []
    )
    vector_ranked = (
        _rank_by_vector(query, documents, embedding_provider, vector_index)
        if retrieval_mode in {"vector", "hybrid"}
        else []
    )
    if retrieval_mode == "bm25":
        ranked = bm25_ranked
    elif retrieval_mode == "vector":
        ranked = vector_ranked
    else:
        ranked = _rank_by_rrf(bm25_ranked, vector_ranked)

    return [
        DocumentSearchHit(
            source_id=item.document.id,
            title=item.document.title,
            document_type=item.document.document_type,
            snippet=_chunk_body(item.text),
            score=item.score,
            metadata=_hit_metadata(item),
        )
        for item in ranked
    ]


def _rank_by_bm25(
    query_tokens: list[str],
    chunks: list[LocalKnowledgeChunk],
) -> list[RankedChunk]:
    tokenized_chunks = [_tokenize(chunk.text) for chunk in chunks]
    doc_freq = Counter(token for tokens in tokenized_chunks for token in set(tokens))
    avg_doc_len = sum(len(tokens) for tokens in tokenized_chunks) / max(
        len(tokenized_chunks), 1
    )
    ranked: list[RankedChunk] = []
    for chunk, tokens in zip(chunks, tokenized_chunks, strict=True):
        score = _bm25_score(query_tokens, tokens, doc_freq, len(chunks), avg_doc_len)
        if score > 0:
            ranked.append(
                RankedChunk(
                    document=chunk.document,
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    score=round(score, 4),
                    bm25_score=round(score, 4),
                )
            )
    ranked.sort(key=lambda item: (-item.score, item.document.id, item.chunk_id))
    return [
        RankedChunk(
            document=item.document,
            chunk_id=item.chunk_id,
            text=item.text,
            score=item.score,
            bm25_score=item.bm25_score,
            bm25_rank=rank,
        )
        for rank, item in enumerate(ranked, start=1)
    ]


def _rank_by_vector(
    query: str,
    documents: list[LocalKnowledgeDocument],
    embedding_provider: EmbeddingProvider,
    vector_index: KnowledgeVectorIndex | None,
) -> list[RankedChunk]:
    if vector_index is None:
        return []
    if vector_index.embedding_model != embedding_provider.model_name:
        return []

    query_embedding = embedding_provider.embed_query(query)
    allowed_document_ids = {document.id for document in documents}
    documents_by_id = {document.id: document for document in documents}
    ranked: list[RankedChunk] = []
    for chunk in _non_redundant_vector_chunks(vector_index.chunks):
        if chunk.document_id not in allowed_document_ids:
            continue
        score = _cosine_similarity(query_embedding, chunk.embedding)
        if score <= 0:
            continue
        ranked.append(
            RankedChunk(
                document=documents_by_id[chunk.document_id],
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                score=round(score, 4),
                vector_score=round(score, 4),
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.document.id, item.chunk_id))
    return [
        RankedChunk(
            document=item.document,
            chunk_id=item.chunk_id,
            text=item.text,
            score=item.score,
            vector_score=item.vector_score,
            vector_rank=rank,
        )
        for rank, item in enumerate(ranked, start=1)
    ]


def _rank_by_rrf(
    bm25_ranked: list[RankedChunk],
    vector_ranked: list[RankedChunk],
) -> list[RankedChunk]:
    by_chunk_id: dict[tuple[int, str], RankedChunk] = {}
    for item in bm25_ranked:
        by_chunk_id[(item.document.id, item.chunk_id)] = item
    for item in vector_ranked:
        key = (item.document.id, item.chunk_id)
        existing = by_chunk_id.get(key)
        if existing is None:
            by_chunk_id[key] = item
            continue
        by_chunk_id[key] = RankedChunk(
            document=existing.document,
            chunk_id=existing.chunk_id,
            text=existing.text,
            score=existing.score,
            bm25_score=existing.bm25_score,
            vector_score=item.vector_score,
            bm25_rank=existing.bm25_rank,
            vector_rank=item.vector_rank,
        )

    fused: list[RankedChunk] = []
    for item in by_chunk_id.values():
        score = 0.0
        if item.bm25_rank is not None:
            score += 1 / (RRF_K + item.bm25_rank)
        if item.vector_rank is not None:
            score += 1 / (RRF_K + item.vector_rank)
        fused.append(
            RankedChunk(
                document=item.document,
                chunk_id=item.chunk_id,
                text=item.text,
                score=round(score, 6),
                bm25_score=item.bm25_score,
                vector_score=item.vector_score,
                bm25_rank=item.bm25_rank,
                vector_rank=item.vector_rank,
            )
        )
    fused.sort(key=lambda item: (-item.score, item.document.id, item.chunk_id))
    return fused


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    doc_freq: Counter[str],
    document_count: int,
    avg_doc_len: float,
) -> float:
    frequencies = Counter(doc_tokens)
    doc_len = len(doc_tokens) or 1
    k1 = 1.5
    b = 0.75
    score = 0.0
    for token in query_tokens:
        frequency = frequencies[token]
        if frequency == 0:
            continue
        idf = math.log(1 + (document_count - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
        denominator = frequency + k1 * (1 - b + b * doc_len / max(avg_doc_len, 1))
        score += idf * (frequency * (k1 + 1) / denominator)
    return score


def _hit_metadata(item: RankedChunk) -> dict:
    metadata = dict(item.document.metadata_json or {})
    metadata["retrieval_debug"] = {
        "bm25_score": item.bm25_score,
        "vector_score": item.vector_score,
        "rrf_score": item.score,
        "bm25_rank": item.bm25_rank,
        "vector_rank": item.vector_rank,
        "chunk_id": item.chunk_id,
        "vector_chunk_id": item.chunk_id if item.vector_rank is not None else None,
    }
    return metadata


def _chunk_body(text: str) -> str:
    # Generated chunks contain title/type/metadata on the first three lines. Those fields are
    # already returned separately and should not crowd out the matched text. Do not apply the
    # old 180-character snippet cap: request.limit now bounds the number of complete chunks.
    body = text.split("\n", 3)[-1].lstrip(" ，。、；;:：")
    return re.sub(r"\s+", " ", body).strip()


def _non_redundant_vector_chunks(
    chunks: list[KnowledgeVectorIndexChunk],
) -> list[KnowledgeVectorIndexChunk]:
    """Drop a trailing overlap-only fragment already contained in the previous chunk."""
    retained: list[KnowledgeVectorIndexChunk] = []
    previous_by_document: dict[int, str] = {}
    for chunk in chunks:
        body = _chunk_body(chunk.text)
        previous = previous_by_document.get(chunk.document_id)
        if previous is not None and len(body) <= CHUNK_OVERLAP and previous.endswith(body):
            continue
        retained.append(chunk)
        previous_by_document[chunk.document_id] = body
    return retained


@lru_cache(maxsize=8)
def _load_local_documents(document_path: Path) -> list[LocalKnowledgeDocument]:
    return [
        LocalKnowledgeDocument.model_validate(item)
        for item in json.loads(document_path.read_text(encoding="utf-8"))
    ]


@lru_cache(maxsize=8)
def _load_vector_index(
    index_path: Path,
    document_path: Path,
) -> KnowledgeVectorIndex | None:
    if not index_path.exists():
        return None
    index = KnowledgeVectorIndex.model_validate_json(index_path.read_text(encoding="utf-8"))
    if index.version != VECTOR_INDEX_VERSION:
        return None
    if index.documents_hash != _documents_hash(document_path):
        return None
    return index


def build_knowledge_vector_index(
    document_path: Path = DEFAULT_KNOWLEDGE_DOCUMENT_PATH,
    index_path: Path = DEFAULT_KNOWLEDGE_VECTOR_INDEX_PATH,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> KnowledgeVectorIndex:
    documents = _load_local_documents(document_path)
    provider = SentenceTransformerEmbeddingProvider(embedding_model)
    chunks = [chunk for document in documents for chunk in _document_chunks(document)]
    embeddings = provider.embed_documents([chunk.text for chunk in chunks])
    index = KnowledgeVectorIndex(
        version=VECTOR_INDEX_VERSION,
        embedding_provider="sentence_transformers",
        embedding_model=embedding_model,
        documents_hash=_documents_hash(document_path),
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        query_instruction=BGE_QUERY_INSTRUCTION,
        chunks=[
            KnowledgeVectorIndexChunk(
                document_id=chunk.document.id,
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                embedding=embedding,
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ],
    )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(index.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _load_vector_index.cache_clear()
    return index


def _documents_hash(document_path: Path) -> str:
    return sha256(document_path.read_bytes()).hexdigest()


def _document_chunks(document: LocalKnowledgeDocument) -> list[LocalKnowledgeChunk]:
    metadata = document.metadata_json or {}
    metadata_text = " ".join(str(value) for value in metadata.values())
    prefix = "\n".join([document.title, document.document_type, metadata_text]).strip()
    content = re.sub(r"\s+", " ", document.content).strip()
    if not content:
        return [
            LocalKnowledgeChunk(
                document=document,
                chunk_id=f"{document.id}:0",
                text=prefix,
            )
        ]

    chunks: list[LocalKnowledgeChunk] = []
    start = 0
    index = 0
    step = max(CHUNK_SIZE - CHUNK_OVERLAP, 1)
    while start < len(content):
        # Do not create an overlap-only tail. For example, a 738-character document with
        # 420/80 chunking is fully covered by chunks starting at 0 and 340; a third chunk at
        # 680 would only duplicate the final 58 characters and could begin mid-sentence.
        if chunks and len(content) - start <= CHUNK_OVERLAP:
            break
        chunk_text = content[start : start + CHUNK_SIZE]
        chunks.append(
            LocalKnowledgeChunk(
                document=document,
                chunk_id=f"{document.id}:{index}",
                text="\n".join([prefix, chunk_text]),
            )
        )
        start += step
        index += 1
    return chunks


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(
        left_value * right_value for left_value, right_value in zip(left, right, strict=True)
    )
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    denominator = left_norm * right_norm
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _tokenize(text: str) -> list[str]:
    normalized = _normalize_text(text)
    tokens = re.findall(r"[a-z0-9][a-z0-9+.-]*", normalized)
    cjk_chars = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    tokens.extend(cjk_chars)
    tokens.extend(_cjk_ngrams(cjk_chars, widths=(2, 3, 4)))
    return [token for token in tokens if token.strip()]


def _cjk_ngrams(chars: list[str], widths: Iterable[int]) -> list[str]:
    ngrams: list[str] = []
    for width in widths:
        ngrams.extend(
            "".join(chars[index : index + width])
            for index in range(max(len(chars) - width + 1, 0))
        )
    return ngrams
