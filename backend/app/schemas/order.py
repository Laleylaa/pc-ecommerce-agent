from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

OrderQueryMode = Literal[
    "explicit",
    "latest",
    "recent",
    "all",
    "remaining",
    "count",
    "page",
    "analysis",
]


class OrderItemCard(BaseModel):
    id: int
    sku_id: int
    sku_name: str
    sku_specs: dict | None
    price: Decimal
    quantity: int


class LogisticsCard(BaseModel):
    express_company: str | None
    logistic_no: str | None
    status: int
    trace: list[dict] = Field(default_factory=list)


class OrderCard(BaseModel):
    id: int
    status: int
    status_label: str
    total_amount: Decimal = Decimal("0")
    pay_amount: Decimal
    freight_amount: Decimal = Decimal("0")
    created_at: datetime
    items: list[OrderItemCard]
    logistics: LogisticsCard | None
    pay_at: datetime | None = None
    delivery_at: datetime | None = None


class OrderSummary(BaseModel):
    id: int
    status: int
    status_label: str
    total_amount: Decimal = Decimal("0")
    pay_amount: Decimal
    created_at: str
    item_count: int
    first_item_name: str | None = None
    express_company: str | None = None
    logistic_no: str | None = None
    latest_logistics_event: dict | None = None


class OrderQueryMeta(BaseModel):
    query_mode: OrderQueryMode
    total_match_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    is_exhaustive: bool
    offset: int = Field(default=0, ge=0)
    next_offset: int | None = Field(default=None, ge=0)
