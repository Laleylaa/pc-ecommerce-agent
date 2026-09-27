from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.catalog import ProductCard
from app.schemas.memory import MemoryChange
from app.schemas.order import OrderCard, OrderQueryMeta, OrderSummary

BoundaryClassificationValue = Literal[
    "in_scope_auto",
    "human_handoff_required",
    "out_of_scope",
    "security_refusal",
]


class EvidenceItem(BaseModel):
    source_type: Literal["knowledge_document"]
    source_id: int
    title: str
    document_type: str
    snippet: str
    score: float | None = None
    metadata: dict = Field(default_factory=dict)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    conversation_id: int | None = None


class SuggestedAction(BaseModel):
    label: str
    payload: dict = Field(default_factory=dict)


class BoundaryClassification(BaseModel):
    classification: BoundaryClassificationValue
    reason: str
    display_message: str


class ChatResponse(BaseModel):
    conversation_id: int
    answer: str
    intent: str
    boundary: BoundaryClassification
    evidence: list[EvidenceItem] = Field(default_factory=list)
    products: list[ProductCard] = Field(default_factory=list)
    order: OrderCard | None = None
    orders: list[OrderSummary] = Field(default_factory=list)
    order_query: OrderQueryMeta | None = None
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    memory_changes: list[MemoryChange] | None = None
