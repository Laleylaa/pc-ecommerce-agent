from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_session
from app.models import AppUser
from app.repositories.orders import OrderRepository
from app.schemas.order import OrderCard

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/latest", response_model=OrderCard)
async def latest_order(
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OrderCard:
    order = await OrderRepository(session).latest_order(current_user.id)
    if order is None:
        raise HTTPException(status_code=404, detail="No order found")
    return order


@router.get("/{order_id}", response_model=OrderCard)
async def get_order(
    order_id: int,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OrderCard:
    order = await OrderRepository(session).get_order(current_user.id, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
