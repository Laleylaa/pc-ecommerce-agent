from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class MemoryItem(BaseModel):
    id: int
    key: str
    fact_type: str
    display_value: str
    structured_value: dict[str, Any]
    origin: str
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None


class MemoryChange(BaseModel):
    action: Literal["created", "updated"]
    memory_id: int
    key: str
    display_value: str
