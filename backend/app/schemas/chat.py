from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

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
    source_type: Literal["knowledge_document", "web_page"]
    source_id: int
    url: str | None = None
    title: str
    document_type: str
    snippet: str
    score: float | None = None
    metadata: dict = Field(default_factory=dict)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str  # Text content (including blank) is handled by backend input rules.
    conversation_id: int | None = None


class SuggestedAction(BaseModel):
    label: str
    payload: dict = Field(default_factory=dict)


class BoundaryClassification(BaseModel):
    classification: BoundaryClassificationValue
    reason: str
    display_message: str


class ExecutionGoal(BaseModel):
    id: str
    query: str
    disposition: str
    task_ids: list[str] = Field(default_factory=list)


class ExecutionToolCall(BaseModel):
    id: str
    task_id: str
    tool_name: str
    query: str
    outcome: str
    reason: str
    used_in_answer: bool = False
    superseded: bool = False
    reused: bool = False
    arguments: dict = Field(default_factory=dict)
    result_summary: dict = Field(default_factory=dict)


class ExecutionDetails(BaseModel):
    goals: list[ExecutionGoal] = Field(default_factory=list)
    tool_calls: list[ExecutionToolCall] = Field(default_factory=list)
    model_calls: dict[str, int] | None = None


class ChatResponse(BaseModel):
    conversation_id: int
    answer: str
    intent: str
    boundary: BoundaryClassification
    evidence: list[EvidenceItem] = Field(default_factory=list)
    products: list[SerializeAsAny[ProductCard]] = Field(default_factory=list)
    order: OrderCard | None = None
    orders: list[OrderSummary] = Field(default_factory=list)
    order_query: OrderQueryMeta | None = None
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    memory_changes: list[MemoryChange] | None = None
    execution_details: ExecutionDetails | None = None
