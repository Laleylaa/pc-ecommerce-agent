from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

HandoffRequestType = Literal["refund", "return", "repair", "order_change", "other"]
HandoffRequestStatus = Literal["pending", "acknowledged", "resolved"]


class CreateAfterSalesRequest(BaseModel):
    session_id: int
    order_id: int | None = None
    request_type: HandoffRequestType
    reason: str = Field(min_length=2, max_length=500)


class CreateAfterSalesTicketRequest(BaseModel):
    order_id: int
    order_item_id: int
    ticket_type: str = Field(pattern="^(return|exchange|refund|repair)$")
    reason: str = Field(min_length=2, max_length=255)
    description: str | None = None
    evidence: list[dict] = Field(default_factory=list)


class AfterSalesTicketCard(BaseModel):
    id: int
    order_id: int
    order_item_id: int
    ticket_type: str
    reason: str
    status: str
    created_at: datetime


class AfterSalesTicketResponse(BaseModel):
    ticket: AfterSalesTicketCard


class HandoffRequestAccepted(BaseModel):
    request_id: int
    status: HandoffRequestStatus
    message: str


class HandoffRequestCard(BaseModel):
    id: int
    session_id: int
    order_id: int | None
    request_type: HandoffRequestType
    reason: str
    boundary_category: str
    status: HandoffRequestStatus
    created_at: datetime
    updated_at: datetime
