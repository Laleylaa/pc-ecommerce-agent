"""Structured semantic review of a tool plan, never a query execution decision."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolPlanReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: Literal["approve", "revise", "uncertain"]
    reason: str = Field(
        default="", max_length=2000,
        description="修正时说明具体差异；无法判断时说明疑点。通过可省略，不要求摘录或路径。",
    )

    @model_validator(mode="after")
    def require_repair_feedback(self) -> "ToolPlanReview":
        # A repair needs feedback, but the program does not certify its semantics.
        if self.verdict == "revise" and not self.reason:
            raise ValueError("revise requires feedback for plan correction")
        return self
