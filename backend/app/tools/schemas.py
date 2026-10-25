from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.llm_feedback import OutputValidationError
from app.schemas.catalog import ProductCard
from app.schemas.catalog_search import CatalogSearchRequest, CategoryHint, FieldIssue, SearchScope
from app.schemas.context import CatalogMemory
from app.schemas.order import OrderCard, OrderFilters, OrderQueryMode, OrderSuggestion, OrderSummary
from app.tools.order_identity import explicit_order_ids
from app.tools.output_limits import MAX_RESULT_ITEMS, BoundedToolOutput

NonBlankQuery = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


def validate_order_lookup_selection(order_id: int | None, query: str | None) -> None:
    if order_id is None and query is None:
        raise OutputValidationError(
            "order lookup requires a nonblank query or an explicit order_id",
        )
    query_ids = explicit_order_ids(query or "")
    if len(query_ids) > 1:
        raise OutputValidationError(
            "one order lookup accepts one explicit order ID; split multiple IDs", path=("query",),
        )
    if order_id is not None and query_ids and query_ids != {order_id}:
        raise OutputValidationError("query order ID conflicts with order_id", path=("query",))


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    recommended_action: str = "stop"
    phase: Literal["plan_validation", "execution"] | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    tool_name: str
    ok: bool
    output: dict | None = None
    error: ToolError | None = None


class ToolDiagnostic(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"] = "info"
    message: str
    recommended_action: str = "use_result"
    details: dict[str, Any] = Field(default_factory=dict)


class CatalogPreferenceDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_usage: list[str] = Field(default_factory=list, max_length=8)
    max_price: Decimal | None = None
    connection_type: Literal["Wireless", "Wired"] | None = None
    usage: str | None = Field(default=None, max_length=64)


class CatalogTargetIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: int | None = Field(default=None, ge=1)
    spu_id: int | None = Field(default=None, ge=1)
    source: Literal[
        "current_turn_artifact",
        "working_memory_reference",
        "explicit_argument",
    ]

    @model_validator(mode="after")
    def require_identity(self) -> "CatalogTargetIdentity":
        if self.sku_id is None and self.spu_id is None:
            raise ValueError("catalog target requires sku_id or spu_id")
        return self


class CatalogDependencyArtifact(BaseModel):
    """A trusted, task-scoped Catalog artifact supplied by the DAG runtime."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    artifact_type: str = ""
    products: list[ProductCard] = Field(default_factory=list, max_length=20)
    selected_sku_ids: list[int] = Field(default_factory=list, max_length=20)
    selected_spu_ids: list[int] = Field(default_factory=list, max_length=20)
    selection_scope: Literal["sku", "spu"] | None = None


class CatalogToolRuntimeContext(BaseModel):
    """Read-only Catalog context hidden from model-authored Tool arguments."""

    model_config = ConfigDict(extra="forbid")

    memory: CatalogMemory = Field(default_factory=CatalogMemory)
    dependency_artifacts: list[CatalogDependencyArtifact] = Field(
        default_factory=list,
        max_length=8,
    )


class CatalogSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    category: str | None = None
    brand: str | None = None
    brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_usage: list[str] = Field(default_factory=list, max_length=8)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    usage: str | None = Field(default=None, max_length=64)
    sort: Literal["recommend", "sales", "price_asc", "price_desc", "stock"] = "recommend"
    targets: list[CatalogTargetIdentity] = Field(default_factory=list, max_length=10)
    catalog_context: CatalogToolRuntimeContext = Field(
        default_factory=CatalogToolRuntimeContext
    )
    preference_defaults: CatalogPreferenceDefaults = Field(
        default_factory=CatalogPreferenceDefaults
    )
    limit: int = Field(default=3, ge=1, le=20)


class CatalogSearchOutput(BaseModel):
    result_type: Literal["products", "empty"]
    result_purpose: Literal[
        "recommendation",
        "search",
        "lookup",
        "ranking",
        "count",
    ] = "search"
    selection_scope: Literal["sku", "spu"] = "spu"
    products: list[ProductCard] = Field(default_factory=list)
    total_match_count: int = Field(default=0, ge=0)
    returned_count: int = Field(default=0, ge=0)
    is_exhaustive: bool = True
    ranking_strategy: str
    query_plan: dict = Field(default_factory=dict)
    diagnostics: list[ToolDiagnostic] = Field(default_factory=list)


class CatalogSeriesSpecValue(BaseModel):
    value: str
    sku_count: int = Field(ge=1)
    in_stock_sku_count: int = Field(ge=0)


class CatalogSeriesSpecSummary(BaseModel):
    present_sku_count: int = Field(ge=1)
    missing_sku_count: int = Field(ge=0)
    values: list[CatalogSeriesSpecValue] = Field(default_factory=list)


class CatalogSeriesVariant(BaseModel):
    sku_id: int
    title: str
    price: Decimal
    stock: int
    sku_sales_count: int = 0
    specs: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None


class CatalogSeriesSummary(BaseModel):
    spu_id: int
    title: str
    brand: str
    category: str
    sales_count: int = 0
    sku_count: int = Field(ge=1)
    in_stock_sku_count: int = Field(ge=0)
    total_stock: int = Field(ge=0)
    min_price: Decimal
    max_price: Decimal
    common_specs: dict[str, str] = Field(default_factory=dict)
    option_specs: dict[str, CatalogSeriesSpecSummary] = Field(default_factory=dict)
    variants: list[CatalogSeriesVariant] = Field(default_factory=list)


class CatalogFacetValue(BaseModel):
    total_sku_ids_count: int | None = Field(default=None, ge=0)
    total_spu_ids_count: int | None = Field(default=None, ge=0)
    sources_exhaustive: bool = True

    value: str
    sku_ids: list[int] = Field(default_factory=list)
    spu_ids: list[int] = Field(default_factory=list)


class CatalogFacetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = ""
    category_hint: CategoryHint = None
    scope: SearchScope | None = None
    facet: Literal["category", "brand", "spec_key", "spec_value"] = "brand"
    category: str | None = None
    brand: str | None = None
    spec_key: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=MAX_RESULT_ITEMS, ge=1, le=MAX_RESULT_ITEMS, strict=True)

    @field_validator("scope")
    @classmethod
    def reject_empty_scope(cls, scope: SearchScope | None) -> SearchScope | None:
        return CatalogSearchRequest.reject_explicit_empty_scope(scope) if scope else None


class CatalogFacetItem(BaseModel):
    total_nested_value_count: int | None = Field(default=None, ge=0)
    values_exhaustive: bool = True

    value: str
    spec_key: str | None = None
    values: list[CatalogFacetValue] = Field(default_factory=list)
    count: int
    count_scope: Literal["sku", "spu"] = "spu"
    sku_count: int = 0
    spu_count: int = 0


class CatalogFacetOutput(BoundedToolOutput):
    result_type: Literal["facets", "empty"]
    facet: Literal["category", "brand", "spec_key", "spec_value"]
    items: list[CatalogFacetItem] = Field(default_factory=list)
    category: str | None = None
    brand: str | None = None
    spec_key: str | None = None
    scope: SearchScope | None = None
    total_value_count: int = Field(default=0, ge=0)
    returned_count: int = Field(default=0, ge=0)
    is_exhaustive: bool = True
    matching_complete: bool = True
    field_issues: list[FieldIssue] = Field(default_factory=list)
    query_plan: dict = Field(default_factory=dict)
    diagnostics: list[ToolDiagnostic] = Field(default_factory=list)


class OrderLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    order_id: int | None = Field(default=None, gt=0, strict=True)
    query: NonBlankQuery | None = Field(default=None, max_length=256)
    limit: int = Field(default=5, ge=1, le=5, strict=True)
    offset: int = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def validate_selection(self) -> "OrderLookupInput":
        validate_order_lookup_selection(self.order_id, self.query)
        return self


class OrderLookupOutput(BoundedToolOutput):
    result_type: Literal[
        "single_order",
        "order_candidates",
        "order_count",
        "order_analysis",
        "order_suggestions",
        "unsupported",
        "invalid_query",
        "planning_failed",
        "not_found",
    ]
    order: OrderCard | None = None
    candidates: list[OrderSummary] = Field(default_factory=list)
    analysis_orders: list[OrderCard] = Field(default_factory=list)
    # Suggestions are never exact matches or automatic downstream binding inputs.
    suggestions: list[OrderSuggestion] = Field(default_factory=list)
    suggestion_filters: OrderFilters | None = None
    suggestion_total_count: int = Field(default=0, ge=0)
    suggestion_is_exhaustive: bool = True
    focus_order_ids: list[int] = Field(default_factory=list)
    query_mode: OrderQueryMode = "recent"
    total_match_count: int = Field(default=0, ge=0)
    returned_count: int = Field(default=0, ge=0)
    is_exhaustive: bool = True
    offset: int = Field(default=0, ge=0)
    next_offset: int | None = Field(default=None, ge=0)
    query_plan: dict = Field(default_factory=dict)
    diagnostics: list[ToolDiagnostic] = Field(default_factory=list)


class DocumentSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: NonBlankQuery
    document_type: str | None = None
    limit: int = Field(default=3, ge=1, le=MAX_RESULT_ITEMS, strict=True)
    retrieval_mode: Literal["bm25", "vector", "hybrid"] = "hybrid"


class DocumentSearchHit(BaseModel):
    source_type: Literal["knowledge_document"] = "knowledge_document"
    source_id: int
    title: str
    document_type: str
    snippet: str
    score: float
    metadata: dict = Field(default_factory=dict)


class DocumentSearchOutput(BoundedToolOutput):
    result_type: Literal["documents", "empty"]
    documents: list[DocumentSearchHit] = Field(default_factory=list)
    search_strategy: Literal["bm25", "vector", "hybrid"] = "hybrid"


class WebSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: NonBlankQuery
    limit: int = Field(default=3, ge=1, le=MAX_RESULT_ITEMS, strict=True)


class WebSearchHit(BaseModel):
    source_type: Literal["web_page"] = "web_page"
    source_id: int
    url: str
    title: str
    document_type: Literal["peripheral_knowledge"] = "peripheral_knowledge"
    snippet: str
    score: float
    metadata: dict = Field(default_factory=dict)


class WebSearchOutput(BoundedToolOutput):
    result_type: Literal["documents", "empty"]
    documents: list[WebSearchHit] = Field(default_factory=list)
    search_strategy: Literal["web"] = "web"
