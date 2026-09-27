from collections.abc import Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppUser,
    Conversation,
    MemoryFact,
    UserAuthCredential,
)
from app.repositories.conversations import ConversationRepository
from app.services.auth import PasswordHasher, normalize_login_identifier


@pytest.mark.asyncio
async def test_auth_rejects_chat_without_bearer_token(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/chat", json={"message": "帮我查最近订单"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_user_cannot_override_chat_user_id(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await api_client.post(
        "/api/chat",
        json={"message": "帮我查最近订单", "user_id": 999},
        headers=auth_headers,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_user_b_cannot_read_user_a_orders_conversations_memory_or_handoff_records(
    api_client: AsyncClient,
    db_session_factory: Callable[[], AsyncSession],
    demo_credentials: tuple[str, str],
) -> None:
    user_b_identifier = "user-b@example.com"
    user_b_password = "user-b-password"
    async with db_session_factory() as session:
        user_b = AppUser(
            login_identifier=normalize_login_identifier(user_b_identifier),
            display_name="User B",
            status="active",
        )
        session.add(user_b)
        await session.flush()
        session.add(
            UserAuthCredential(
                user_id=user_b.id,
                login_identifier=user_b.login_identifier,
                password_hash=PasswordHasher.hash_password(user_b_password),
            )
        )
        session.add(
            MemoryFact(
                user_id=1,
                scope="user",
                fact_type="preference",
                key="test_isolation_preference",
                value="wireless",
                confidence=0.9,
            )
        )
        await session.commit()
        user_b_id = user_b.id

    login_identifier, password = demo_credentials
    user_a_headers = await _login_headers(api_client, login_identifier, password)
    user_b_headers = await _login_headers(api_client, user_b_identifier, user_b_password)

    user_a_chat = await api_client.post(
        "/api/chat",
        json={"message": "推荐无线鼠标"},
        headers=user_a_headers,
    )
    assert user_a_chat.status_code == 200
    user_a_conversation_id = user_a_chat.json()["conversation_id"]

    handoff_response = await api_client.post(
        "/api/after-sales",
        json={
            "session_id": user_a_conversation_id,
            "order_id": 202607020001,
            "request_type": "return",
            "reason": "A 用户的人工接管记录",
        },
        headers=user_a_headers,
    )
    assert handoff_response.status_code == 202
    handoff_request_id = handoff_response.json()["request_id"]

    order_response = await api_client.get(
        "/api/orders/202607020001?user_id=1",
        headers=user_b_headers,
    )
    assert order_response.status_code == 404

    user_b_chat = await api_client.post(
        "/api/chat",
        json={"message": "帮我查最近订单", "conversation_id": user_a_conversation_id},
        headers=user_b_headers,
    )
    assert user_b_chat.status_code == 200
    assert user_b_chat.json()["conversation_id"] != user_a_conversation_id
    user_b_conversation_id = user_b_chat.json()["conversation_id"]

    conversations_response = await api_client.get("/api/conversations", headers=user_b_headers)
    assert conversations_response.status_code == 200
    conversation_ids = {item["id"] for item in conversations_response.json()}
    assert user_b_conversation_id in conversation_ids
    assert user_a_conversation_id not in conversation_ids

    forbidden_conversation = await api_client.get(
        f"/api/conversations/{user_a_conversation_id}",
        headers=user_b_headers,
    )
    assert forbidden_conversation.status_code == 404

    handoff_query_response = await api_client.get(
        f"/api/after-sales/handoff-requests/{handoff_request_id}",
        headers=user_b_headers,
    )
    assert handoff_query_response.status_code == 404

    async with db_session_factory() as session:
        user_a_conversation = await session.get(Conversation, user_a_conversation_id)
        assert user_a_conversation is not None
        assert user_a_conversation.user_id == 1

        user_b_conversation = await session.get(
            Conversation,
            user_b_chat.json()["conversation_id"],
        )
        assert user_b_conversation is not None
        assert user_b_conversation.user_id == user_b_id

        user_a_memory = (
            await session.execute(select(MemoryFact).where(MemoryFact.user_id == 1))
        ).scalars()
        assert user_a_memory.first() is not None
        user_b_memory = await ConversationRepository(session).list_memory(user_b_id)
        assert user_b_memory == []

        handoff_user_id = (
            await session.execute(
                text("SELECT user_id FROM handoff_request WHERE id = :request_id"),
                {"request_id": handoff_request_id},
            )
        ).scalar_one()
        assert handoff_user_id == 1


async def _login_headers(
    api_client: AsyncClient,
    login_identifier: str,
    password: str,
) -> dict[str, str]:
    response = await api_client.post(
        "/api/auth/login",
        json={"login_identifier": login_identifier, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
