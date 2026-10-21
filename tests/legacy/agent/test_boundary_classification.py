
from typing import cast

import pytest
from app.agent.boundary import BOUNDARY_POLICY
from app.agent.intent import classify_boundary
from app.agent.legacy.responses import MIXED_HANDOFF_CONFIRMATION
from app.agent.legacy.runtime import AgentRuntime
from app.core.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_handoff_answer_uses_boundary_template() -> None:
    boundary = classify_boundary("帮我创建售后工单")
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = {
        "message": "帮我创建售后工单",
        "intent": "after_sales",
        "boundary": boundary.model_dump(mode="json"),
        "parsed": {},
    }

    result = await runtime._render_handoff_template(state)

    assert result["answer"] == boundary.display_message
    assert "帮我创建售后工单" not in result["answer"]
    assert result["answer"].endswith("是否需要转人工客服？")
    assert result["suggested_actions"] == [
        {
            "label": "转人工客服",
            "payload": {
                "handoff": True,
                "orderId": None,
                "requestType": "other",
                "reason": "帮我创建售后工单",
            },
        }
    ]


@pytest.mark.asyncio
async def test_handoff_answer_preserves_direct_part_and_uses_mixed_template() -> None:
    runtime = AgentRuntime(cast(AsyncSession, None), Settings(llm_api_key=""))
    state = {
        "message": "你是谁，另外帮我取消订单",
        "route_plan": {
            "rewritten_query": "说明客服身份，另外取消订单",
            "subqueries": [
                {
                    "id": "sq_1",
                    "query": "你是谁",
                    "disposition": "direct_response",
                    "reason_code": "identity",
                },
                {
                    "id": "sq_2",
                    "query": "帮我取消订单",
                    "disposition": "human_handoff",
                    "reason_code": "cancel_order",
                },
            ],
        },
        "parsed": {},
    }

    result = await runtime._render_handoff_template(state)

    assert result["answer"].startswith("我是小帕瓦")
    assert result["answer"].endswith(MIXED_HANDOFF_CONFIRMATION)
    assert (
        BOUNDARY_POLICY.for_classification(
            "human_handoff_required"
        ).display_message
        not in result["answer"]
    )
    assert result["boundary"]["classification"] == "human_handoff_required"
    assert result["suggested_actions"][0]["payload"]["handoff"] is True
