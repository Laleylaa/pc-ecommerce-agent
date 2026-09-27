from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AfterSalesEvent, AfterSalesTicket, Conversation, HandoffRequest
from app.repositories.orders import OrderRepository
from app.schemas.after_sales import (
    AfterSalesTicketCard,
    CreateAfterSalesRequest,
    CreateAfterSalesTicketRequest,
    HandoffRequestCard,
)


class AfterSalesRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_ticket(
        self, user_id: int, request: CreateAfterSalesTicketRequest
    ) -> AfterSalesTicketCard | None:
        orders = OrderRepository(self.session)
        if not await orders.item_belongs_to_user(user_id, request.order_id, request.order_item_id):
            return None

        ticket = AfterSalesTicket(
            user_id=user_id,
            order_id=request.order_id,
            order_item_id=request.order_item_id,
            ticket_type=request.ticket_type,
            reason=request.reason,
            description=request.description,
            evidence_json=request.evidence,
            status="submitted",
        )
        self.session.add(ticket)
        await self.session.flush()
        self.session.add(
            AfterSalesEvent(
                ticket_id=ticket.id,
                actor="customer",
                event_type="submitted",
                note=request.reason,
                metadata_json={"description": request.description},
            )
        )
        await self.session.commit()
        await self.session.refresh(ticket)
        return AfterSalesTicketCard(
            id=ticket.id,
            order_id=ticket.order_id,
            order_item_id=ticket.order_item_id,
            ticket_type=ticket.ticket_type,
            reason=ticket.reason,
            status=ticket.status,
            created_at=ticket.created_at,
        )

    async def list_tickets(self, user_id: int) -> list[AfterSalesTicketCard]:
        stmt = (
            select(AfterSalesTicket)
            .where(AfterSalesTicket.user_id == user_id)
            .order_by(AfterSalesTicket.created_at.desc())
        )
        tickets = (await self.session.execute(stmt)).scalars().all()
        return [_to_ticket_card(ticket) for ticket in tickets]

    async def get_ticket(self, user_id: int, ticket_id: int) -> AfterSalesTicketCard | None:
        stmt = select(AfterSalesTicket).where(
            AfterSalesTicket.user_id == user_id,
            AfterSalesTicket.id == ticket_id,
        )
        ticket = (await self.session.execute(stmt)).scalar_one_or_none()
        return _to_ticket_card(ticket) if ticket else None


def _to_ticket_card(ticket: AfterSalesTicket) -> AfterSalesTicketCard:
    return AfterSalesTicketCard(
        id=ticket.id,
        order_id=ticket.order_id,
        order_item_id=ticket.order_item_id,
        ticket_type=ticket.ticket_type,
        reason=ticket.reason,
        status=ticket.status,
        created_at=ticket.created_at,
    )


class HandoffRequestRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_request(
        self,
        user_id: int,
        request: CreateAfterSalesRequest,
        boundary_category: str,
    ) -> HandoffRequestCard | None:
        conversation_exists = (
            await self.session.execute(
                select(Conversation.id).where(
                    Conversation.id == request.session_id,
                    Conversation.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if conversation_exists is None:
            return None

        if request.order_id is not None:
            order = await OrderRepository(self.session).get_order(user_id, request.order_id)
            if order is None:
                return None

        handoff_request = HandoffRequest(
            user_id=user_id,
            session_id=request.session_id,
            order_id=request.order_id,
            request_type=request.request_type,
            reason=request.reason,
            boundary_category=boundary_category,
            status="pending",
        )
        self.session.add(handoff_request)
        await self.session.commit()
        await self.session.refresh(handoff_request)
        return _to_handoff_request_card(handoff_request)

    async def get_request(self, user_id: int, request_id: int) -> HandoffRequestCard | None:
        handoff_request = (
            await self.session.execute(
                select(HandoffRequest).where(
                    HandoffRequest.user_id == user_id,
                    HandoffRequest.id == request_id,
                )
            )
        ).scalar_one_or_none()
        return _to_handoff_request_card(handoff_request) if handoff_request else None


def _to_handoff_request_card(handoff_request: HandoffRequest) -> HandoffRequestCard:
    return HandoffRequestCard(
        id=handoff_request.id,
        session_id=handoff_request.session_id,
        order_id=handoff_request.order_id,
        request_type=handoff_request.request_type,
        reason=handoff_request.reason,
        boundary_category=handoff_request.boundary_category,
        status=handoff_request.status,
        created_at=handoff_request.created_at,
        updated_at=handoff_request.updated_at,
    )
