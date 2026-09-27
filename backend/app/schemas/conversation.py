from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ConversationSummary(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    last_message: str | None = None
    last_message_role: Literal["user", "assistant"] | None = None
    last_message_at: datetime | None = None


class ConversationMessageItem(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    metadata: dict | None = None
    created_at: datetime


class ConversationDetail(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageItem] = Field(default_factory=list)
