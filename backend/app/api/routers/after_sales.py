from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intent import classify_boundary
from app.api.dependencies import get_current_user
from app.core.database import get_session
from app.models import AppUser
from app.repositories.support import AfterSalesRepository, HandoffRequestRepository
from app.schemas.after_sales import (
    AfterSalesTicketCard,
    CreateAfterSalesRequest,
    HandoffRequestAccepted,
    HandoffRequestCard,
)

router = APIRouter(prefix="/after-sales", tags=["after-sales"])

REQUEST_TYPE_LABELS = {
    "refund": "退款",
    "return": "退货",
    "repair": "维修",
    "order_change": "订单修改",
    "other": "售后人工确认",
}


@router.get("", response_model=list[AfterSalesTicketCard])
async def list_after_sales_tickets(
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AfterSalesTicketCard]:
    return await AfterSalesRepository(session).list_tickets(current_user.id)


@router.get("/handoff-requests/{request_id}", response_model=HandoffRequestCard)
async def get_handoff_request(
    request_id: int,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HandoffRequestCard:
    handoff_request = await HandoffRequestRepository(session).get_request(
        current_user.id,
        request_id,
    )
    if handoff_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff request not found",
        )
    return handoff_request


@router.get("/{ticket_id}", response_model=AfterSalesTicketCard)
async def get_after_sales_ticket(
    ticket_id: int,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AfterSalesTicketCard:
    ticket = await AfterSalesRepository(session).get_ticket(current_user.id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


@router.post("", response_model=HandoffRequestAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_after_sales_ticket(
    request: CreateAfterSalesRequest,
    current_user: AppUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HandoffRequestAccepted:
    request_type_label = REQUEST_TYPE_LABELS.get(request.request_type, "售后人工确认")
    boundary = classify_boundary(f"我要申请{request_type_label}，原因：{request.reason}")
    handoff_request = await HandoffRequestRepository(session).create_request(
        current_user.id,
        request,
        boundary.classification,
    )
    if handoff_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation or order not found",
        )
    return HandoffRequestAccepted(
        request_id=handoff_request.id,
        status=handoff_request.status,
        message="请求已记录，系统不会自动办理业务操作，请等待人工确认。",
    )
