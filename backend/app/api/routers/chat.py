import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.api.dependencies import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.models import AppUser
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    return await AgentRuntime(session, settings).run(request, current_user.id)


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        async for event in AgentRuntime(session, settings).run_stream(request, current_user.id):
            event_type = event.get("type", "message")
            payload = json.dumps(event, ensure_ascii=False, default=str)
            yield f"event: {event_type}\ndata: {payload}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
