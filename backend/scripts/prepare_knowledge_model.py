"""Warm the local embedding model without rewriting knowledge data or database records."""

from app.tools.knowledge import (
    DEFAULT_KNOWLEDGE_DOCUMENT_PATH,
    DEFAULT_KNOWLEDGE_VECTOR_INDEX_PATH,
    SentenceTransformerEmbeddingProvider,
    _load_vector_index,
)


def main() -> None:
    index = _load_vector_index(DEFAULT_KNOWLEDGE_VECTOR_INDEX_PATH, DEFAULT_KNOWLEDGE_DOCUMENT_PATH)
    if index is None:
        raise SystemExit("Knowledge index missing or stale. Run make knowledge-index first.")
    provider = SentenceTransformerEmbeddingProvider(allow_download=True)
    if index.embedding_model != provider.model_name:
        raise SystemExit("Knowledge index uses a different model. Run make knowledge-index.")
    embedding = provider.embed_query("本地知识检索模型检查")
    if any(len(chunk.embedding) != len(embedding) for chunk in index.chunks):
        raise SystemExit("Knowledge index dimensions differ from model; rebuild the index.")
    print(f"Knowledge model ready: {provider.model_name} ({len(embedding)} dimensions)")


if __name__ == "__main__":
    main()
