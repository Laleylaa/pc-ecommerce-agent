"""Structured semantic review of a tool plan, never a query execution decision."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequirementReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: str = Field(min_length=1, max_length=2000)
    plan_paths: list[str] = Field(max_length=12)
    status: Literal["matched", "unresolved", "missing", "mismatched"]
    explanation: str = Field(min_length=1, max_length=1000)


class ToolPlanReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["approve", "revise", "uncertain"]
    checks: list[RequirementReview] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def consistent_verdict(self) -> "ToolPlanReview":
        problems = any(c.status in {"missing", "mismatched"} for c in self.checks)
        if self.verdict == "approve" and problems:
            raise ValueError("approve cannot contain missing or mismatched requirements")
        if self.verdict == "revise" and not problems:
            raise ValueError("revise requires a specific missing or mismatched requirement")
        if any(c.status in {"matched", "unresolved"} and not c.plan_paths for c in self.checks):
            raise ValueError("matched/unresolved requirements need a plan path")
        return self
