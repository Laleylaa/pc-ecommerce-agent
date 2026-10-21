"""Model-facing loop structures; no natural-language interpretation or execution."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.boundary import RouteDisposition

Identifier = Annotated[str, Field(min_length=1, max_length=64)]
SourceText = Annotated[str, Field(min_length=1, max_length=2000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CandidateReference(StrictModel):
    kind: Literal["catalog_candidate", "order_candidate"]
    position: int = Field(ge=1, le=20)


class SelectedReference(StrictModel):
    kind: Literal["referenced_product", "last_order"]


ContextReference = Annotated[
    CandidateReference | SelectedReference, Field(discriminator="kind")
]


class GoalPrerequisite(StrictModel):
    goal_id: Identifier
    requirement: Literal["product_found", "order_found", "sources_found", "condition"]
    source_text: SourceText


class QueryGoal(StrictModel):
    id: Identifier
    source_text: SourceText
    query: SourceText
    disposition: RouteDisposition
    prerequisites: list[GoalPrerequisite] = Field(default_factory=list, max_length=8)
    references: list[ContextReference] = Field(default_factory=list, max_length=20)


class GoalRegistration(StrictModel):
    goals: list[QueryGoal] = Field(min_length=1, max_length=8)


class QueryCompletion(StrictModel):
    """Model decision to stop querying, never a factual success certificate."""

    reason: str = Field(min_length=1, max_length=500, pattern=r"\S")


class RegistrationCall(StrictModel):
    registration: GoalRegistration


class CompletionCall(StrictModel):
    completion: QueryCompletion


class GoalToolCall(StrictModel):
    """SDK envelope; arguments are validated with the selected formal tool schema."""

    goal_id: Identifier
    arguments: dict[str, Any]
