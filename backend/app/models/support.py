from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AfterSalesTicket(Base):
    __tablename__ = "after_sales_ticket"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("order_info.id"), nullable=False)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_item.id"), nullable=False)
    ticket_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted")
    evidence_json: Mapped[dict | list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AfterSalesEvent(Base):
    __tablename__ = "after_sales_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("after_sales_ticket.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class HandoffRequest(Base):
    __tablename__ = "handoff_request"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("order_info.id"))
    request_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    boundary_category: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
