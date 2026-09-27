from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_session
from app.models import AppUser
from app.repositories.conversations import ConversationRepository
from app.schemas.memory import MemoryItem

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("", response_model=list[MemoryItem])
async def list_memories(
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MemoryItem]:
    memories = await ConversationRepository(session).list_memory(current_user.id, limit=None)
    return [
        MemoryItem(
            id=memory.id,
            key=memory.key,
            fact_type=memory.fact_type,
            display_value=memory.value,
            structured_value=memory.value_json,
            origin=memory.origin,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            last_used_at=memory.last_used_at,
        )
        for memory in memories
    ]


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_memory(
    memory_id: int,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    disabled = await ConversationRepository(session).disable_memory(current_user.id, memory_id)
    if not disabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
