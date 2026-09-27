from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.memory import MemoryChange

SAFE_QUERY_PLAN_KEYS = {
    "brands",
    "category",
    "excluded_brands",
    "excluded_usage",
    "fallback_reason",
    "filters",
    "keywords",
    "limit",
    "max_price",
    "min_price",
    "planner",
    "query",
    "selection_scope",
    "sort",
    "supported",
    "unsupported_reason",
    "usage_scenario",
}
SAFE_QUERY_FILTER_KEYS = {
    "backlit",
    "channels",
    "color",
    "connection_type",
    "enclosure_type",
    "field_of_view",
    "frame_rate",
    "frequency_response",
    "hand_orientation",
    "max_dpi",
    "microphone",
    "panel_type",
    "power_w",
    "refresh_rate",
    "resolution",
    "response_time_ms",
    "size_inch",
    "style",
    "switches",
    "tenkeyless",
    "tracking_method",
    "type",
    "weight_g",
    "wireless",
}


class CatalogDisplayIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spu_id: int
    sku_id: int
    title: str
    entity_scope: Literal["sku", "spu"] = "sku"
    brand: str | None = None
    category: str | None = None
    image_url: str | None = None


class CatalogComparisonMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    comparison_level: Literal["sku", "spu"] = "sku"
    sku_ids: list[int] = Field(default_factory=list)
    spu_ids: list[int] = Field(default_factory=list)
    comparison_fields: list[str] = Field(default_factory=list)

    @field_validator("sku_ids", "spu_ids", mode="before")
    @classmethod
    def unique_comparison_ids(cls, value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(int(item) for item in value if item is not None))[:10]


class CatalogMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_plan: dict[str, Any] = Field(default_factory=dict)
    comparison: CatalogComparisonMemory = Field(default_factory=CatalogComparisonMemory)
    candidate_spu_ids: list[int] = Field(default_factory=list)
    candidate_sku_ids: list[int] = Field(default_factory=list)
    candidate_display: list[CatalogDisplayIdentity] = Field(default_factory=list)
    referenced_spu_id: int | None = None
    referenced_sku_id: int | None = None

    @field_validator("query_plan", mode="before")
    @classmethod
    def strip_volatile_query_values(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        sanitized = {key: item for key, item in value.items() if key in SAFE_QUERY_PLAN_KEYS}
        filters = sanitized.get("filters")
        if isinstance(filters, dict):
            sanitized["filters"] = {
                key: item
                for key, item in filters.items()
                if key in SAFE_QUERY_FILTER_KEYS
                and (item is None or isinstance(item, (str, int, float, bool)))
            }
        elif "filters" in sanitized:
            sanitized["filters"] = {}
        return sanitized

    @field_validator("candidate_spu_ids", "candidate_sku_ids", mode="before")
    @classmethod
    def unique_identifiers(cls, value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(int(item) for item in value if item is not None))


class OrderMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_order_id: int | None = None
    candidate_order_ids: list[int] = Field(default_factory=list)
    total_match_count: int = Field(default=0, ge=0)
    returned_count: int = Field(default=0, ge=0)
    is_exhaustive: bool = True
    next_offset: int | None = Field(default=None, ge=0)

    @field_validator("candidate_order_ids", mode="before")
    @classmethod
    def unique_order_ids(cls, value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(int(item) for item in value if item is not None))[:20]


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_type: str
    source_id: int
    title: str | None = None
    document_type: str | None = None


class PolicyMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_query: str | None = None
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)


class HandoffMemory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: int | None = None
    request_type: str | None = None
    reason: str | None = None


class WorkingMemoryV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    catalog: CatalogMemory = Field(default_factory=CatalogMemory)
    order: OrderMemory = Field(default_factory=OrderMemory)
    policy: PolicyMemory = Field(default_factory=PolicyMemory)
    handoff: HandoffMemory = Field(default_factory=HandoffMemory)


class ContextMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class HistorySelection(BaseModel):
    messages: list[ContextMessage] = Field(default_factory=list)
    estimated_token_count: int = 0
    retained_turns: int = 0
    dropped_turns: int = 0


class StructuredMemory(BaseModel):
    id: int
    scope: str
    fact_type: str
    key: str
    value: str
    value_json: dict[str, Any]
    confidence: float


class PreparedTurn(BaseModel):
    user_id: int
    conversation_id: int
    user_message_id: int
    run_id: int
    message: str
    history: list[ContextMessage] = Field(default_factory=list)
    memory: list[StructuredMemory] = Field(default_factory=list)
    working_memory: WorkingMemoryV2 = Field(default_factory=WorkingMemoryV2)
    estimated_token_count: int = 0
    retained_turns: int = 0
    dropped_turns: int = 0


class MemoryChanges(BaseModel):
    working_memory: WorkingMemoryV2
    upserted_memory_ids: list[int] = Field(default_factory=list)
    applied_memory_ids: list[int] = Field(default_factory=list)
    memory_changes: list[MemoryChange] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)


def upgrade_working_memory(value: dict[str, Any] | None) -> WorkingMemoryV2:
    if not value:
        return WorkingMemoryV2()
    if value.get("schema_version") == 2:
        return WorkingMemoryV2.model_validate(value)

    recent_products = value.get("recent_products")
    products = recent_products if isinstance(recent_products, list) else []
    referenced = value.get("last_referenced_product")
    referenced_product = referenced if isinstance(referenced, dict) else {}
    evidence = value.get("recent_evidence")
    evidence_items = evidence if isinstance(evidence, list) else []
    handoff = value.get("pending_handoff")

    return WorkingMemoryV2(
        catalog=CatalogMemory(
            query_plan=value.get("current_product_search") or {},
            candidate_spu_ids=_collect_ids(products, "spu_id"),
            candidate_sku_ids=_collect_ids(products, "sku_id"),
            candidate_display=[
                CatalogDisplayIdentity.model_validate(item)
                for item in products
                if isinstance(item, dict)
                and item.get("spu_id") is not None
                and item.get("sku_id") is not None
                and item.get("title")
            ],
            referenced_spu_id=_optional_int(referenced_product.get("spu_id")),
            referenced_sku_id=_optional_int(referenced_product.get("sku_id")),
        ),
        order=OrderMemory(last_order_id=_optional_int(value.get("last_order_id"))),
        policy=PolicyMemory(
            last_query=value.get("last_policy_query"),
            evidence_refs=[
                EvidenceReference.model_validate(item)
                for item in evidence_items
                if isinstance(item, dict)
                and item.get("source_type") is not None
                and item.get("source_id") is not None
            ],
        ),
        handoff=HandoffMemory.model_validate(handoff or {}),
    )


def _collect_ids(items: list[Any], key: str) -> list[int]:
    return list(
        dict.fromkeys(
            int(item[key])
            for item in items
            if isinstance(item, dict) and item.get(key) is not None
        )
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None
