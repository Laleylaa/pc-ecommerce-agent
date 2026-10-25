from app.tools.knowledge import (
    KnowledgeRetrievalToolService,
    KnowledgeVectorIndex,
    KnowledgeVectorIndexChunk,
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
