from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class ProductSpecCondition(BaseModel):
    key: str
    operator: Literal["exact", "eq", "in", "gte", "lte"] = "eq"
    values: list[str] = Field(min_length=1)


class ProductSearchRequest(BaseModel):
    query: str = ""
    spu_ids: list[int] = Field(default_factory=list, max_length=10)
    category: str | None = None
    brands: list[str] = Field(default_factory=list, max_length=8)
    usage_scenario: str | None = None
    usage_required_conditions: list[ProductSpecCondition] = Field(default_factory=list)
    usage_preferred_conditions: list[ProductSpecCondition] = Field(default_factory=list)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    excluded_brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_usage: list[str] = Field(default_factory=list, max_length=8)
    sort: Literal["recommend", "sales", "price_asc", "price_desc", "stock"] = "recommend"
    limit: int = Field(default=8, ge=1, le=20)


class ProductVariantCard(BaseModel):
    sku_id: int
    title: str
    price: Decimal
    stock: int
    sku_sales_count: int = 0
    specs: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None


class ProductCard(BaseModel):
    spu_id: int
    sku_id: int
    title: str
    spu_title: str | None = None
    entity_scope: Literal["sku", "spu"] = "sku"
    brand: str
    category: str
    price: Decimal
    stock: int
    sku_sales_count: int = 0
    sku_sales_count_scope: Literal["sku"] = "sku"
    sales_count: int = 0
    sales_count_scope: Literal["spu"] = "spu"
    specs: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None
    ranking_scope: Literal["sku", "spu"] | None = None
    ranking_metric: Literal["price", "stock", "sales"] | None = None
    ranking_value: Decimal | None = None
    series_min_price: Decimal | None = None
    series_max_price: Decimal | None = None
    series_total_stock: int | None = None
    series_sku_count: int | None = None
    series_common_specs: dict[str, str] = Field(default_factory=dict)
    series_option_specs: dict[str, list[str]] = Field(default_factory=dict)
    series_variants: list[ProductVariantCard] = Field(default_factory=list)


class ProductSearchResponse(BaseModel):
    products: list[ProductCard]
