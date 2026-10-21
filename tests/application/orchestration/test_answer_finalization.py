

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.api.routers import chat as chat_api
from app.core.config import Settings
from app.repositories.conversations import ConversationRepository
from app.schemas.chat import ChatRequest
from app.services.context import ConversationContextService
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_context_commits_accepted_request_before_answer_transaction():
    session = SimpleNamespace(commit=AsyncMock())
    repo = SimpleNamespace(
        get_or_create=AsyncMock(return_value=SimpleNamespace(id=10)),
        list_recent_messages=AsyncMock(return_value=[]),
        count_complete_turns=AsyncMock(return_value=0),
        get_working_memory=AsyncMock(return_value={}),
        list_memory=AsyncMock(return_value=[]),
        add_message=AsyncMock(return_value=SimpleNamespace(id=20)),
        start_run=AsyncMock(return_value=SimpleNamespace(id=30)),
    )
    service = ConversationContextService(session, Settings(llm_api_key=""), repository=repo)
    prepared = await service.prepare_turn(7, None, "查询键盘")
    assert (prepared.conversation_id, prepared.user_message_id, prepared.run_id) == (10, 20, 30)
    session.commit.assert_awaited_once()
    repo.add_message.assert_awaited_once_with(10, "user", "查询键盘")


@pytest.mark.asyncio
async def test_failure_audit_does_not_overwrite_a_completed_run():
    run = SimpleNamespace(status="completed", intent="catalog_search", state_json={"saved": True})
    repo = ConversationRepository(SimpleNamespace(get=AsyncMock(return_value=run)))
    await repo.fail_run(1, "error", {}, {"type": "post_commit_failure"})
    assert run.status == "completed" and run.state_json == {"saved": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,status",
    [
        (TimeoutError("private timeout details"), 504),
        (RuntimeError("private write details"), 500),
        (HTTPException(status_code=403, detail="无权访问会话"), 403),
    ],
)
async def test_sync_api_returns_controlled_errors_and_preserves_access_denial(
    monkeypatch,
    failure,
    status,
):
    runtime = SimpleNamespace(run=AsyncMock(side_effect=failure))
    monkeypatch.setattr(chat_api, "create_runtime", lambda *args: runtime)
    with pytest.raises(HTTPException) as caught:
        await chat_api.chat(
            ChatRequest(message="查询键盘"),
            SimpleNamespace(id=7),
            None,
            Settings(llm_api_key=""),
        )
    assert caught.value.status_code == status
    assert "private" not in caught.value.detail
