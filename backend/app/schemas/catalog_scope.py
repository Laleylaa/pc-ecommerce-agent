"""Scope decisions precede executable specifications; no catalog facts are model-authored."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.catalog_search import ProductReference, QueryText


class CatalogScopePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_kind: Literal["catalog", "category", "candidates", "object", "spu", "sku"] = Field(
        description=(
            "查询范围：全店、品类、可信候选、层级待确认的指定对象、指定型号或具体版本。"
            "只描述查谁/在哪查，不判定工具职责；名称身份和实际层级由程序查库确认。"
        ),
    )
    category: QueryText | None
    search_text: QueryText | None
    target: ProductReference | None
    needs_specifications: bool = Field(strict=True)
    reason: str = ""
