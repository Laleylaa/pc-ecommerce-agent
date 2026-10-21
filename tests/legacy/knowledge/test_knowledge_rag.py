


from app.agent.legacy.responses import _fallback_answer
from app.schemas.chat import EvidenceItem


def test_after_sales_fallback_answer_includes_knowledge_evidence() -> None:
    evidence = EvidenceItem(
        source_type="knowledge_document",
        source_id=1,
        title="七天无理由退货政策",
        document_type="policy",
        snippet="自签收次日起七天内，商品未影响二次销售，可申请七天无理由退货。",
        score=0.42,
        metadata={"scenario": "return"},
    )
    state = {"evidence": [evidence]}

    answer = _fallback_answer(state)

    assert "知识库" in answer
    assert "七天无理由退货政策" in answer


def test_multi_document_fallback_does_not_dump_raw_snippets() -> None:
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
    state = {"evidence": evidence}

    answer = _fallback_answer(state)

    assert "暂时无法可靠归纳出直接答案" in answer
    assert "raw snippet" not in answer
