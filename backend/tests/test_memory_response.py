from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.schemas.context import MemoryChanges, PreparedTurn

EXPECTED_CHANGE = {
    "action": "created",
    "memory_id": 88,
    "key": "brand_preference",
    "display_value": "偏好罗技品牌",
}


class _ContextWithMemoryChange:
    async def prepare_turn(
        self, user_id: int, conversation_id: int | None, message: str
    ) -> PreparedTurn:
        return PreparedTurn(
            user_id=user_id,
            conversation_id=conversation_id or 41,
            user_message_id=51,
            run_id=61,
            message=message,
        )

    async def complete_turn(
        self, prepared_turn: PreparedTurn, outcome: dict[str, Any]
    ) -> MemoryChanges:
        return MemoryChanges.model_validate(
            {
                "working_memory": prepared_turn.working_memory,
                "upserted_memory_ids": [88],
                "memory_changes": [EXPECTED_CHANGE],
            }
        )


@pytest.mark.asyncio
async def test_memory_changes_serialize_in_sync_and_sse_done_responses() -> None:
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        context_service=cast(Any, _ContextWithMemoryChange()),
    )
    request = ChatRequest(message="今天天气怎么样")

    sync_response = await runtime.run(request, user_id=7)
    stream_events = [event async for event in runtime.run_stream(request, user_id=7)]

    assert sync_response.model_dump(mode="json")["memory_changes"] == [EXPECTED_CHANGE]
    assert stream_events[-1]["type"] == "done"
    assert stream_events[-1]["response"]["memory_changes"] == [EXPECTED_CHANGE]
