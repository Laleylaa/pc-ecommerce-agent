"""Internal local-library path, not the public web knowledge_search tool."""
import pytest
from app.tools.schemas import DocumentSearchInput

from tests.tools.shared.local_retrieval.fixtures import _document_service


@pytest.mark.asyncio
async def test_knowledge_search_eval_can_return_brand_docs() -> None:
    result = await _document_service().search_knowledge(
        DocumentSearchInput(query="Logitech 罗技 品牌", retrieval_mode="bm25", limit=5)
    )

    assert result.result_type == "documents"
    assert result.documents[0].document_type == "brand"
    assert result.documents[0].source_id == 2



@pytest.mark.asyncio
async def test_knowledge_search_eval_rejects_policy_document_type_filter() -> None:
    result = await _document_service().search_knowledge(
        DocumentSearchInput(
            query="退货政策", document_type="policy", retrieval_mode="bm25", limit=5
        )
    )

    assert result.result_type == "empty"
    assert result.documents == []
