"""Internal model sharing and local-library hybrid retrieval checks."""
import app.tools.knowledge as knowledge_tools
import pytest
from app.tools.schemas import (
    DocumentSearchInput,
)

from tests.tools.shared.local_retrieval.hybrid_fixtures import (
    _knowledge_service,
)


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
