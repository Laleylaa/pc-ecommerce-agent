import pytest
from app.tools.schemas import DocumentSearchInput

from tests.tools.shared.local_retrieval.fixtures import _document_service


@pytest.mark.asyncio
async def test_policy_search_eval_prefers_policy_and_faq_types() -> None:
    result = await _document_service().search_policy(
        DocumentSearchInput(query="退货退款保修政策", retrieval_mode="bm25", limit=5)
    )

    assert result.result_type == "documents"
    assert result.documents[0].document_type == "policy"
    assert {item.document_type for item in result.documents} <= {"policy", "store_rule", "faq"}



@pytest.mark.asyncio
async def test_policy_search_eval_rejects_brand_document_type_filter() -> None:
    result = await _document_service().search_policy(
        DocumentSearchInput(
            query="Logitech 品牌", document_type="brand", retrieval_mode="bm25", limit=5
        )
    )

    assert result.result_type == "empty"
    assert result.documents == []
