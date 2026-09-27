from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="新会话")
    working_memory_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Message(Base):
    __tablename__ = "message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AgentRun(Base):
    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    intent: Mapped[str | None] = mapped_column(String(64))
    state_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    completed_at: Mapped[datetime | None]


class ToolCall(Base):
    __tablename__ = "tool_call"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(ForeignKey("agent_run.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict | None] = mapped_column(JSONB)
    output_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class MemoryFact(Base):
    __tablename__ = "memory_fact"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    fact_type: Mapped[str] = mapped_column(String(64), nullable=False, default="preference")
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[dict | None] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy_inferred", server_default="legacy_inferred"
    )
    confidence: Mapped[float] = mapped_column(default=0.7, server_default="0.7")
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    disabled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_document"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chroma_collection: Mapped[str | None] = mapped_column(String(128))
    chroma_id: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
