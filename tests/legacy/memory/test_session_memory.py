from typing import cast

from app.agent.legacy.runtime import _request_router_messages
from app.agent.legacy.state import AgentState
from langchain_core.messages import AIMessage, HumanMessage


def test_request_router_messages_include_session_history_before_current_request() -> None:
    state = cast(
        AgentState,
        {
            "message": "给我下这个一来一回标准的 json 格式",
            "intent": "general",
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "属于 PC 外设商城客服范围，优先进入自动应答流程",
                "display_message": "可自动回答",
            },
            "memory": [],
            "working_memory": {},
            "history": [
                {
                    "role": "user",
                    "content": (
                        "一般我们跟 LLM 对话，都是 user:xxx assistant:xxx "
                        "这样一来一回对吧？"
                    ),
                },
                {
                    "role": "assistant",
                    "content": "是的，这种交互模式通常称为 chat format。",
                },
            ],
            "evidence": [],
            "products": [],
            "order": None,
        },
    )

    messages = _request_router_messages(state)

    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == state["history"][0]["content"]
    assert isinstance(messages[2], AIMessage)
    assert messages[2].content == state["history"][1]["content"]
    assert isinstance(messages[3], HumanMessage)
    assert "给我下这个一来一回标准的 json 格式" in messages[3].content
    assert "<original_request>" in messages[3].content
