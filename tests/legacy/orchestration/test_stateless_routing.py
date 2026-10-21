from typing import cast

from app.agent.legacy.runtime import _suggest_actions
from app.agent.legacy.state import AgentState


def test_handoff_uses_order_id_from_current_message() -> None:
    state = cast(
        AgentState,
        {
            "message": "订单 202607020001 我要退货",
            "boundary": {
                "classification": "human_handoff_required",
                "reason": "涉及售后、订单变更或其他需要人工确认的写操作",
                "display_message": "这个请求需要人工客服确认后处理。",
            },
            "intent": "after_sales",
        },
    )

    actions = _suggest_actions(state)

    assert actions == [
        {
            "label": "转人工客服",
            "payload": {
                "handoff": True,
                "orderId": 202607020001,
                "requestType": "return",
                "reason": "订单 202607020001 我要退货",
            },
        }
    ]
