from typing import cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import AgentRuntime, _request_router_messages
from app.agent.state import AgentState
from app.core.config import Settings
from app.repositories.conversations import ConversationRepository


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


@pytest.mark.asyncio
async def test_load_context_reads_existing_conversation_messages_as_session_history(
    db_session_factory,
) -> None:
    async with db_session_factory() as session:
        repo = ConversationRepository(session)
        conversation = await repo.get_or_create(1, None)
        await repo.add_message(conversation.id, "user", "上一轮用户问题")
        await repo.add_message(conversation.id, "assistant", "上一轮助手回答")
        await session.commit()

        runtime = AgentRuntime(session, Settings(llm_api_key=""))
        state = await runtime._load_context(
            cast(
                AgentState,
                {
                    "user_id": 1,
                    "conversation_id": conversation.id,
                    "message": "继续按刚才的格式给我示例",
                },
            )
        )

    assert state["history"] == [
        {"role": "user", "content": "上一轮用户问题"},
        {"role": "assistant", "content": "上一轮助手回答"},
    ]
    assert "working_memory" not in state
    assert "memory" not in state


@pytest.mark.asyncio
async def test_load_context_only_reads_six_most_recent_messages(
    db_session_factory,
) -> None:
    async with db_session_factory() as session:
        repo = ConversationRepository(session)
        conversation = await repo.get_or_create(1, None)
        for index in range(7):
            role = "user" if index % 2 == 0 else "assistant"
            await repo.add_message(conversation.id, role, f"历史消息 {index}")
        await session.commit()

        runtime = AgentRuntime(session, Settings(llm_api_key=""))
        state = await runtime._load_context(
            cast(
                AgentState,
                {
                    "user_id": 1,
                    "conversation_id": conversation.id,
                    "message": "当前用户输入",
                },
            )
        )

    assert state["history"] == [
        {"role": "assistant", "content": "历史消息 1"},
        {"role": "user", "content": "历史消息 2"},
        {"role": "assistant", "content": "历史消息 3"},
        {"role": "user", "content": "历史消息 4"},
        {"role": "assistant", "content": "历史消息 5"},
        {"role": "user", "content": "历史消息 6"},
    ]
    assert all(item["content"] != "当前用户输入" for item in state["history"])
