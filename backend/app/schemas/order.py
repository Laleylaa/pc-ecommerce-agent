from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class OrderFilters(BaseModel):
    """One filter contract for order counts, details and paged lists.

    Aware boundaries use Asia/Shanghai wall time for naive database columns.
    Ranges are [start_at, end_at).
    Product terms must occur in the same purchased item, not across items.
    """

    model_required_fields: ClassVar[tuple[str, ...]] = (
        "time_field",
        "start_at",
        "end_at",
        "statuses",
        "product_terms",
    )
    model_config = ConfigDict(extra="forbid")

    time_field: Literal["created_at", "pay_at", "delivery_at"] = "created_at"
    start_at: datetime | None = Field(default=None, strict=True)
    end_at: datetime | None = Field(default=None, strict=True)
    statuses: list[Annotated[int, Field(strict=True, ge=1, le=5)]] = Field(
        default_factory=list, max_length=5
    )
    product_terms: list[
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100, strict=True)
        ]
    ] = Field(default_factory=list, max_length=12)

    @field_validator("start_at", "end_at")
    @classmethod
    def local_database_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        return value

    @property
    def active(self) -> bool:
        return bool(self.start_at or self.end_at or self.statuses or self.product_terms)

    @property
    def conflicting(self) -> bool:
        return bool(self.start_at and self.end_at and self.start_at >= self.end_at)


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


class OrderConditionDifference(BaseModel):
    field: Literal["time_range", "statuses"]
    expected: dict[str, Any]
    actual: str | int | None
    state: Literal["not_matched", "unknown"]


class OrderSuggestion(BaseModel):
    order: OrderCard
    differences: list[OrderConditionDifference] = Field(min_length=1)


class PendingOrderSuggestion(BaseModel):
    id: int
    differences: list[OrderConditionDifference]
