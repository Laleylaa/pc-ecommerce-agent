"""Scope decisions precede executable specifications; no catalog facts are model-authored."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.catalog_search import ProductReference, QueryText


class CatalogScopePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: Literal["catalog", "object"]
    category: QueryText | None
    search_text: QueryText | None
    target: ProductReference | None
    needs_specifications: bool = Field(strict=True)
    disposition: Literal["ready", "clarification_required", "split_required", "unsupported"]
    reason: str = ""
