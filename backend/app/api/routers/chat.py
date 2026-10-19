
from fastapi import APIRouter, Depends, HTTPException
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
    try:
        return await AgentRuntime(session, settings).run(request, current_user.id)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="本次处理超时，请稍后重试。") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="AI 回复生成失败，请稍后重试。") from exc
