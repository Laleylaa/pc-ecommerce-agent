from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_session
from app.models import AppUser
from app.repositories.conversations import ConversationRepository
from app.schemas.conversation import ConversationDetail, ConversationSummary

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationSummary]:
    return await ConversationRepository(session).list_conversations(current_user.id)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: int,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationDetail:
    conversation = await ConversationRepository(session).get_detail(
        current_user.id,
        conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    deleted = await ConversationRepository(session).delete_conversation(
        current_user.id, conversation_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await session.commit()
