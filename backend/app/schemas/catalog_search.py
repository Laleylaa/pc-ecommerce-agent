"""Stateless, single-goal catalog search contract (no conversation memory)."""

from decimal import Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.core.llm_feedback import OutputValidationError
from app.schemas.catalog import ProductCard, ProductVariantCard
from app.tools.catalog.fields import hard_condition_verdict, normalize_category_hint
from app.tools.catalog.limits import MAX_DISPLAY_COUNT, MAX_IDENTITY_SUGGESTIONS
from app.tools.catalog.outcomes import (
    LookupStatus,
    QueryState,
    ResultAction,
    ResultReason,
    ResultStatus,
    completed_policy,
    validate_policy_fields,
)
from app.tools.catalog.outcomes import (
    data_quality as assess_data_quality,
)
from app.tools.output_limits import BoundedToolOutput

QueryText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000)
]
PositiveId = Annotated[int, Field(strict=True, gt=0)]
Count = Annotated[int, Field(strict=True, ge=0)]
CategoryHint = Annotated[
    Annotated[str, StringConstraints(strict=True, strip_whitespace=True, max_length=40)] | None,
    AfterValidator(normalize_category_hint),
    Field(
        description="可选品类提示，保留兼容；不是筛选条件或权威品类。工具根据原请求和实际对象加载规格规则，不确定时省略。"
    ),
]


class SearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "description": "Use SKU IDs OR SPU IDs, never both nonempty. Preserve object granularity.",
        "not": {"required": ["sku_ids", "spu_ids"], "properties": {
            "sku_ids": {"minItems": 1}, "spu_ids": {"minItems": 1},
        }},
    })

    sku_ids: list[PositiveId] = Field(default_factory=list, max_length=50)
    spu_ids: list[PositiveId] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_ids(self) -> "SearchScope":
        if any(value <= 0 for value in self.sku_ids + self.spu_ids):
            raise ValueError("scope identifiers must be positive")
        if self.sku_ids and self.spu_ids:
            raise PydanticCustomError(
                "scope_type_conflict", "use one scope type: sku_ids or spu_ids",
            )
        self.sku_ids = list(dict.fromkeys(self.sku_ids))
        self.spu_ids = list(dict.fromkeys(self.spu_ids))
        return self


class CatalogSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: QueryText = Field(description="完整的单目标商品查询")
    category_hint: CategoryHint = None
    scope: SearchScope = Field(default_factory=SearchScope)
    requested_count: int | None = Field(
        default=None, ge=1, strict=True,
        description="调用方从用户要求确定的总数量；无明确数字或要求全部时省略。工具内部模型不可改写。",
    )
    limit: int = Field(
        default=3,
        ge=1,
        le=MAX_DISPLAY_COUNT,
        strict=True,
        description=(
            "单次展示上限，最大10，不等于用户要求数量；明确要N款时requested_count=N、limit=min(N,10)。"
            "要求全部时limit=10；没有数量要求时省略，默认最多3款。"
        ),
    )
    offset: int = Field(default=0, ge=0, le=10000, strict=True)

    @field_validator("scope")
    @classmethod
    def reject_explicit_empty_scope(cls, scope: SearchScope) -> SearchScope:
        if not scope.sku_ids and not scope.spu_ids:
            raise OutputValidationError(
                "explicit scope must contain IDs; omit scope for the whole catalog",
            )
        return scope


class ProductReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["sku", "spu"]
    id: PositiveId


class SearchCondition(BaseModel):
    model_required_fields: ClassVar[tuple[str, ...]] = (
        "key", "operator", "values", "strength",
    )

    model_config = ConfigDict(extra="forbid")

    key: str
    operator: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte"] = Field(
        default="eq",
        description="以上必须gte（含本数），以下必须lte（含本数）；gt仅超过，lt仅低于；不要把以上写成gt",
    )
    values: list[str] = Field(min_length=1, max_length=12)
    evidence: str = Field(
        default="", max_length=2000,
        description="可选的要求说明，仅供审计；不是执行依据，不要求逐字摘录。",
    )
    strength: Literal["requirement", "preference"] = "requirement"
    preference_evidence: str | None = Field(
        default=None, max_length=2000,
        description="可选的偏好说明，仅供审计；普通规格默认requirement。",
    )

    @model_validator(mode="after")
    def validate_arity(self) -> "SearchCondition":
        if self.operator not in {"in", "not_in"} and len(self.values) != 1:
            raise OutputValidationError("this operator takes exactly one value", path=("values",))
        return self


class UnresolvedCatalogRequirement(BaseModel):
    """Preserve absent specifications, subjective ambiguity or unsupported tasks."""

    model_config = ConfigDict(extra="forbid")
    evidence: QueryText = Field(description="未销售的具体规格、需澄清用途或任务边界，可简要转述。")
    reason: Literal[
        "spec_not_carried", "filter_data_unavailable",
        "requirement_needs_clarification", "request_not_supported",
    ]
    strength: Literal["requirement", "preference"] = "requirement"


def matching_conditions(
    conditions: list[SearchCondition], requirements: list[UnresolvedCatalogRequirement],
) -> list[SearchCondition]:
    """Represent reviewed absent specifications as explicit nonmatching dimensions.

    Legacy filter_data_unavailable declarations use the same complete-catalog policy.
    These keys are evidence dimensions, never executable database fields.
    """
    return [*conditions, *[
        SearchCondition(
            key=f"not_carried_spec:{index}", values=["是"],
            evidence=requirement.evidence, strength=requirement.strength,
        )
        for index, requirement in enumerate(requirements)
        if requirement.reason in {"spec_not_carried", "filter_data_unavailable"}
    ]]


class SearchPlan(BaseModel):
    # Only execution decisions are mandatory on the wire; audit prose is optional.
    model_required_fields: ClassVar[tuple[str, ...]] = (
        "operation", "disposition", "conditions", "requested_fields",
        "unresolved_requirements",
    )
    model_optional_on_stop: ClassVar[tuple[str, tuple[str, ...], tuple[str, ...]]] = (
        "disposition", ("unsupported", "clarification_required", "split_required"),
        ("requested_fields", "candidate_unit"),
    )

    model_config = ConfigDict(extra="forbid")

    @property
    def matching_conditions(self) -> list[SearchCondition]:
        return matching_conditions(self.conditions, self.unresolved_requirements)

    operation: Literal["search", "lookup"] = "search"
    version_view: Literal["summary", "list"] = Field(
        default="summary",
        description="指定商品默认汇总并展示低/中/高价代表版本；明确列版本用list。",
    )
    candidate_unit: Literal["spu", "sku"] = Field(
        default="spu",
        description=(
            "search比较与计数单位：普通推荐spu，先同SKU匹配再按型号分组；"
            "只有明确选购具体版本时sku。规格条件、实际购买SKU及候选数量不决定此值。"
        ),
    )
    disposition: Literal["ready", "split_required", "clarification_required", "unsupported"] = (
        "ready"
    )
    reason: str = ""
    category: str | None = Field(
        default=None,
        description="只取query明说的品类；名称查询通常null，不能从型号、面板、声道、功率或hint推断",
    )
    category_evidence: str | None = Field(
        default=None, max_length=2000, description="可选的品类说明，仅供审计。"
    )
    search_text: QueryText | None = Field(
        default=None,
        description="名称检索文本：品牌、型号、标题类型词或版本名；不重复已结构化的规格条件，不猜型号。",
    )
    target: ProductReference | None = Field(
        default=None,
        description="仅用于query中明确写出的SKU/SPU编号；可与同一对象的search_text共同限定身份，不要复制scope编号"
    )
    conditions: list[SearchCondition] = Field(default_factory=list, max_length=24)
    unresolved_requirements: list[UnresolvedCatalogRequirement] = Field(
        default_factory=list, max_length=12,
        description="未销售规格用spec_not_carried并保留查询；用途模糊用requirement_needs_clarification；不删除要求。",
    )
    requested_fields: list[str] = Field(default_factory=list, max_length=24)
    unsupported_requirements: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="兼容既有计划的未支持筛选要求；新计划使用unresolved_requirements，可省略本字段。",
    )
    clarification_requirements: list[QueryText] = Field(
        default_factory=list,
        max_length=12,
        description="兼容既有计划的待澄清要求；新计划使用unresolved_requirements，可省略本字段。",
    )
    unsupported_reason: Literal[
        ResultReason.REQUEST_NOT_SUPPORTED, ResultReason.DATA_INSUFFICIENT,
        ResultReason.FILTER_DATA_UNAVAILABLE,
    ] = ResultReason.REQUEST_NOT_SUPPORTED
    sort: Literal["relevance", "price_asc", "price_desc", "sales", "stock"] = "relevance"
    sort_evidence: str | None = Field(
        default=None, max_length=2000, description="可选的排序说明，仅供审计。",
    )
    purpose: Literal["search", "lookup", "recommendation"] = "search"
    requested_limit: int | None = Field(default=None, ge=1, strict=True)


class CatalogDiscoveryPlan(SearchPlan):
    model_required_fields: ClassVar[tuple[str, ...]] = (
        *SearchPlan.model_required_fields, "candidate_unit",
    )
    operation: Literal["search"] = "search"
    purpose: Literal["search", "recommendation"] = "search"
    version_view: Literal["summary"] = Field(
        default="summary",
        description=(
            "search固定为summary，不控制候选卡片粒度；candidate_unit=sku仍返回具体SKU版本卡片。"
            "summary/list版本事实视图切换仅适用于lookup。"
        ),
    )


class CatalogLookupPlan(SearchPlan):
    operation: Literal["lookup"] = "lookup"
    purpose: Literal["lookup"] = "lookup"
    candidate_unit: Literal["spu"] = "spu"


class FieldIssue(BaseModel):
    sku_id: int
    field: str
    status: Literal["missing", "conflict"]
    values: list[str] = Field(default_factory=list)


class ConditionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku_id: PositiveId
    condition: SearchCondition
    actual: str | None = None
    verdict: Literal["match", "mismatch", "unknown"]


class SearchProduct(ProductCard):
    model_config = ConfigDict(extra="forbid")

    match_kind: Literal["exact", "alternative", "object_facts"] = "exact"
    condition_checks: list[ConditionCheck] = Field(default_factory=list)
    representative_sku_id: PositiveId | None = None
    description: str | None = None
    candidate_sku_ids: list[int] = Field(
        default_factory=list,
        description="本次明细/预览中的SKU编号；完整匹配数量看series_sku_count。",
    )
    model_sku_count: Count = 0
    model_min_price: Decimal | None = None
    model_max_price: Decimal | None = None
    field_issues: list[FieldIssue] = Field(default_factory=list)
    variant_details_complete: bool = Field(
        default=True,
        description="SKU明细是否覆盖匹配范围；false时版本数量与价格汇总仍描述完整匹配集。",
    )

    @model_validator(mode="after")
    def validate_identity_and_variants(self) -> "SearchProduct":
        ids = self.candidate_sku_ids
        if not ids or len(ids) != len(set(ids)) or any(value <= 0 for value in ids):
            raise ValueError("matched SKU IDs must be nonempty, positive and unique")
        if self.entity_scope == "sku":
            if ids != [self.sku_id] or self.representative_sku_id is not None:
                raise ValueError("SKU results preserve exactly one identity")
        else:
            variants = self.series_variants
            if self.representative_sku_id != self.sku_id or self.sku_id not in ids:
                raise ValueError("SPU compatibility SKU fields must identify their representative")
            if {v.sku_id for v in variants} != set(ids) or len(variants) != len(ids):
                raise ValueError("SPU variants must exactly cover the matching subset")
            if self.series_sku_count is None or self.series_sku_count < len(ids):
                raise ValueError("series count cannot be smaller than its detail preview")
            if self.variant_details_complete != (self.series_sku_count == len(ids)):
                raise ValueError("detail completeness must agree with the series count")
            if self.variant_details_complete and self.series_total_stock != sum(
                v.stock for v in variants
            ):
                raise ValueError("series stock must describe matching variants only")
            representative = next(v for v in variants if v.sku_id == self.sku_id)
            if (self.price, self.stock, self.sku_sales_count, self.image_url) != (
                representative.price,
                representative.stock,
                representative.sku_sales_count,
                representative.image_url,
            ):
                raise ValueError("compatibility SKU fields must describe the representative")
            if self.specs != self.series_common_specs or any(
                any(v.specs.get(key) != value for v in variants)
                for key, value in self.series_common_specs.items()
            ):
                raise ValueError("common specs must hold for every matching variant")
            if self.variant_details_complete and (self.series_min_price, self.series_max_price) != (
                min(v.price for v in variants),
                max(v.price for v in variants),
            ):
                raise ValueError("series prices must describe the matching variants")
        if self.model_sku_count < (self.series_sku_count or len(ids)):
            raise ValueError("model count cannot be smaller than its matching subset")
        return self


class PriceFacts(BaseModel):
    """A price aggregate over a resolved object, not a list of SKU candidates."""

    model_config = ConfigDict(extra="forbid")

    target: ProductReference
    spu_id: PositiveId
    title: str
    brand: str
    category: str
    queried_fields: list[Literal["price"]] = Field(
        default_factory=lambda: ["price"], min_length=1, max_length=1,
    )
    name_constraint: str | None = None
    scope: SearchScope = Field(default_factory=SearchScope)
    applied_conditions: list[SearchCondition] = Field(default_factory=list)
    population: Literal["matching_active_versions"] = "matching_active_versions"
    sku_count: PositiveId
    min_price: Decimal
    max_price: Decimal
    complete: bool

    @model_validator(mode="after")
    def validate_range(self) -> "PriceFacts":
        if self.min_price > self.max_price:
            raise ValueError("price bounds must be ordered")
        if self.target.kind == "sku" and (
            self.sku_count != 1 or self.min_price != self.max_price
        ):
            raise ValueError("one SKU has one price")
        if self.target.kind == "spu" and self.target.id != self.spu_id:
            raise ValueError("SPU identity must agree with the aggregate")
        return self


class ObjectLookupResult(BaseModel):
    target: ProductReference | None = None
    name: QueryText | None = None
    status: LookupStatus

    @model_validator(mode="after")
    def validate_identity(self) -> "ObjectLookupResult":
        if self.target is None and self.name is None:
            raise ValueError("lookup result requires an ID or name identity")
        if self.status == "identity_mismatch" and (self.target is None or self.name is None):
            raise ValueError("identity mismatch requires both the ID and name evidence")
        return self


class SearchDiagnostic(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"] = "info"
    message: str
    recommended_action: str = "use_result"
    details: dict = Field(default_factory=dict)


class IdentitySuggestion(BaseModel):
    """A name to confirm, never a selected product or a condition-qualified result."""

    model_config = ConfigDict(extra="forbid")
    spu_id: PositiveId
    title: str
    brand: str
    category: str
    match_type: Literal["ambiguous", "similar"]
    score: float = Field(ge=0, le=1)
    matched_terms: list[str] = Field(default_factory=list)
    unmatched_terms: list[str] = Field(default_factory=list)
    conditions_verified: Literal[False] = False


class CatalogVersionIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku_id: PositiveId
    title: str


class LookupFieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    versions: list[CatalogVersionIdentity]
    total_version_count: Count | None = None
    versions_exhaustive: bool = True


class LookupFieldFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    values: list[LookupFieldValue] = Field(default_factory=list)
    missing_sku_count: Count = 0
    conflicted_sku_count: Count = 0


class CatalogLookupSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    population: Literal["matching_active_versions"] = "matching_active_versions"
    exists: bool | None
    matching_complete: bool
    matched_sku_count: Count
    unknown_sku_count: Count
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    price_level_count: Count = 0
    fields: list[LookupFieldFacts] = Field(default_factory=list)
    representative_versions: list[ProductVariantCard] = Field(default_factory=list, max_length=3)


class CatalogSearchResult(BoundedToolOutput):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["5"] = "5"
    lookup_summary: CatalogLookupSummary | None = None
    match_kind: Literal["exact", "alternative", "object_facts", "none"] = "none"
    fallback_attempted: bool = False
    identity_match_count: Count | None = None
    candidate_sku_count: Count | None = None
    candidate_spu_count: Count | None = None
    result_type: Literal["products", "facts", "empty"]
    status: ResultStatus
    reason_code: ResultReason
    recommended_action: ResultAction
    retryable: bool = False
    query_state: QueryState = "not_executed"
    result_purpose: Literal["search", "lookup", "recommendation"] = "search"
    selection_scope: Literal["sku", "spu"] | None = None
    products: list[SearchProduct] = Field(default_factory=list)
    facts: PriceFacts | None = Field(
        default=None,
        description="兼容历史价格事实；当前lookup统一返回lookup_summary和商品资料。",
    )
    identity_suggestions: list[IdentitySuggestion] = Field(
        default_factory=list, max_length=MAX_IDENTITY_SUGGESTIONS,
    )
    requested_count: PositiveId | None = None
    display_cap: int = Field(
        default=MAX_DISPLAY_COUNT, strict=True, ge=MAX_DISPLAY_COUNT, le=MAX_DISPLAY_COUNT,
    )
    limit_capped: bool = False
    object_results: list[ObjectLookupResult] = Field(default_factory=list, max_length=1)
    returned_count: Count = 0
    total_match_count: Count | None = Field(
        default=None, description="确认满足全部硬条件的对象数；软偏好只影响排序",
    )
    matched_spu_count: Count | None = None
    matched_sku_count: Count | None = None
    unknown_sku_count: Count | None = Field(
        default=None, description="硬条件无法确认的SKU数；软偏好未知不计入准入不确定",
    )
    candidate_count: Count | None = None
    matching_complete: bool | None = None
    is_exhaustive: bool | None = None
    offset: Count | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_DISPLAY_COUNT)] | None = None
    next_offset: Count | None = None
    data_quality: Literal["not_assessed", "clear", "issues"] = "not_assessed"
    conflicted_sku_count: Count | None = None
    missing_fact_sku_count: Count | None = None
    ranking_strategy: str = "not_executed"
    query_plan: dict = Field(default_factory=dict)
    diagnostics: list[SearchDiagnostic] = Field(default_factory=list)
    planner: str = ""

    @model_validator(mode="after")
    def validate_result_contract(self) -> "CatalogSearchResult":
        validate_policy_fields(
            status=self.status,
            reason=self.reason_code,
            action=self.recommended_action,
            retryable=self.retryable,
            query_state=self.query_state,
        )
        if self.lookup_summary is not None:
            summary = self.lookup_summary
            if self.query_state != "completed" or self.query_plan.get("operation") != "lookup":
                raise ValueError("lookup summary requires an executed object query")
            if summary.matched_sku_count != self.matched_sku_count:
                raise ValueError("summary cannot include alternative versions")
            if summary.unknown_sku_count != self.unknown_sku_count:
                raise ValueError("summary must retain unknown conditions")
            if summary.matching_complete != self.matching_complete:
                raise ValueError("summary completeness must match the full matching population")
            expected_exists = True if summary.matched_sku_count else (
                None if summary.unknown_sku_count else False
            )
            if summary.exists != expected_exists:
                raise ValueError("existence must distinguish missing evidence from no match")
            if not summary.matched_sku_count and (
                summary.min_price is not None or summary.max_price is not None
                or summary.price_level_count or summary.representative_versions or summary.fields
            ):
                raise ValueError(
                    "empty exact population cannot contain facts or alternative prices"
                )
            if summary.min_price is not None or summary.max_price is not None:
                if (summary.min_price is None or summary.max_price is None
                    or summary.min_price > summary.max_price
                    or not 1 <= summary.price_level_count <= summary.matched_sku_count):
                    raise ValueError("requested price statistics must be complete")
            elif summary.price_level_count:
                raise ValueError("price level count requires price statistics")
        if self.status == ResultStatus.INVALID_QUERY:
            conflicts = [d for d in self.diagnostics if d.code == self.reason_code]
            if not conflicts:
                raise ValueError("invalid query requires structured conflict evidence")
            for diagnostic in conflicts:
                key = diagnostic.details.get("field")
                conditions = diagnostic.details.get("conditions")
                if not key or not isinstance(conditions, list) or not conditions:
                    raise ValueError("conflict requires a field and its conditions")
                for condition in conditions:
                    if SearchCondition.model_validate(condition).key != key:
                        raise ValueError("conflict conditions must belong to the stated field")
                    if condition not in self.query_plan.get("conditions", []):
                        raise ValueError("conflict evidence must preserve planned conditions")
        if self.facts is not None and (self.products or self.identity_suggestions):
            raise ValueError("object facts cannot be mixed with candidate lists")
        if self.facts is not None:
            if self.query_plan.get("operation") != "lookup":
                raise ValueError("price facts require object lookup")
            if self.selection_scope != self.facts.target.kind:
                raise ValueError("price facts must preserve the resolved identity")
            if self.facts.sku_count != self.matched_sku_count:
                raise ValueError("price population must cover the complete eligible set")
            if self.facts.complete != self.matching_complete:
                raise ValueError("price completeness must preserve unknown matches")
        if self.returned_count != len(self.products) + int(self.facts is not None):
            raise ValueError("returned count must match the returned collection")
        kind = "facts" if self.facts is not None else "products" if self.products else "empty"
        if self.result_type != kind:
            raise ValueError("result type must describe the returned material")
        counts = [
            self.total_match_count,
            self.matched_spu_count,
            self.matched_sku_count,
            self.unknown_sku_count,
            self.conflicted_sku_count,
            self.missing_fact_sku_count,
            self.candidate_count,
            self.candidate_spu_count,
            self.candidate_sku_count,
        ]
        if self.query_state != "completed":
            if self.identity_suggestions or self.requested_count is not None or self.limit_capped:
                raise ValueError("stopped query has no search suggestions or applied count")
            if (
                any(value is not None for value in counts)
                or self.products or self.facts is not None
            ):
                raise ValueError("unexecuted matching cannot report matches or zero counts")
            if self.matching_complete is not None or self.is_exhaustive is not None:
                raise ValueError("unexecuted matching has unknown completeness")
            if self.data_quality != "not_assessed":
                raise ValueError("match quality is unassessed until matching completes")
            if self.object_results or self.identity_match_count is not None:
                raise ValueError("stopped query cannot claim retrieved objects")
            if self.match_kind != "none" or self.fallback_attempted:
                raise ValueError("stopped query cannot claim a match or fallback")
            if any(
                v is not None
                for v in (self.offset, self.limit, self.next_offset, self.selection_scope)
            ):
                raise ValueError("stopped query has no applied window or selection scope")
            return self
        else:
            if any(value is None for value in counts):
                raise ValueError("completed query requires known counts")
            if self.match_kind == "exact":
                if not self.total_match_count or self.fallback_attempted:
                    raise ValueError("exact results require strict matches and no fallback")
                if (self.candidate_count, self.candidate_spu_count, self.candidate_sku_count) != (
                    self.total_match_count,
                    self.matched_spu_count,
                    self.matched_sku_count,
                ):
                    raise ValueError("exact candidate counts must equal strict match counts")
            elif self.total_match_count or self.matched_sku_count or self.matched_spu_count:
                raise ValueError("non-exact candidates cannot claim strict matches")
            if self.match_kind == "none" and self.candidate_count:
                raise ValueError("no candidates cannot have a positive count")
            if self.match_kind in {"alternative", "object_facts"} and not self.candidate_count:
                raise ValueError("partial results require a nonempty candidate pool")
            if self.match_kind == "alternative" and (
                not self.fallback_attempted or self.unknown_sku_count
            ):
                raise ValueError("alternatives require a conclusive empty strict search")
            if self.candidate_spu_count > self.candidate_sku_count:
                raise ValueError("candidate model count exceeds version count")
            if self.candidate_count != (
                self.candidate_sku_count
                if self.selection_scope == "sku"
                else self.candidate_spu_count
            ):
                raise ValueError("candidate total must use selection granularity")
            if self.matched_spu_count > self.matched_sku_count:
                raise ValueError("model count exceeds version count")
            if self.total_match_count != (
                self.matched_sku_count if self.selection_scope == "sku" else self.matched_spu_count
            ):
                raise ValueError("total count must use the selection granularity")
            identity_pending = bool(self.identity_suggestions)
            if identity_pending and (self.products or self.match_kind != "none"):
                raise ValueError("unconfirmed identities cannot be selected products")
            if self.matching_complete != (self.unknown_sku_count == 0 and not identity_pending):
                raise ValueError("matching completeness must reflect uncertain versions")
            if self.is_exhaustive != (
                self.match_kind in {"exact", "none"}
                and self.matching_complete
                and self.offset == 0
                and self.returned_count == self.total_match_count
            ):
                raise ValueError("exhaustive requires the complete confirmed set in this response")
            if (
                max(self.conflicted_sku_count, self.missing_fact_sku_count)
                > self.candidate_sku_count
            ):
                raise ValueError("quality counts describe selected candidate versions only")
            quality = assess_data_quality(self.conflicted_sku_count, self.missing_fact_sku_count)
            if self.data_quality != quality:
                raise ValueError("quality status must match quality counts")
            expected = completed_policy(
                total_matches=self.total_match_count,
                returned_count=self.returned_count if self.match_kind == "exact" else 0,
                alternative_count=self.candidate_count if self.match_kind == "alternative" else 0,
                object_fact_count=self.candidate_count if self.match_kind == "object_facts" else 0,
                unknown_count=self.unknown_sku_count,
                identity_pending=bool(self.identity_suggestions),
                object_status=self.object_results[0].status if self.object_results else None,
                named_target_missing=(
                    self.reason_code == ResultReason.TARGET_NOT_FOUND
                    and bool(self.query_plan.get("search_text") or self.query_plan.get("name"))
                    and not self.unknown_sku_count
                ),
            )
            if self.reason_code != expected.reason:
                raise ValueError("result policy must agree with completed query facts")
            if len({p.spu_id for p in self.products}) > self.candidate_spu_count:
                raise ValueError("page contains more models than candidates")
            if any(p.entity_scope != self.selection_scope for p in self.products):
                raise ValueError("product granularity must agree with selection scope")
            for product in self.products:
                if product.match_kind != self.match_kind:
                    raise ValueError("card match kind must agree with the result")
                conditions = matching_conditions(
                    [SearchCondition.model_validate(c)
                     for c in self.query_plan.get("conditions", [])],
                    [UnresolvedCatalogRequirement.model_validate(r)
                     for r in self.query_plan.get("unresolved_requirements", [])],
                )
                for sku_id in product.candidate_sku_ids:
                    checks = [c for c in product.condition_checks if c.sku_id == sku_id]
                    if [c.condition for c in checks] != conditions:
                        raise ValueError("each candidate must preserve every planned condition")
                    hard_checks = [c for c in checks if c.condition.strength == "requirement"]
                    if self.match_kind == "exact" and hard_condition_verdict(
                        (c.condition.strength, c.verdict) for c in checks
                    ) != "match":
                        raise ValueError("exact candidates require confirmed hard conditions")
                    if self.match_kind == "alternative":
                        failed = {c.condition.key for c in hard_checks if c.verdict == "mismatch"}
                        if (
                            len(failed) != 1
                            or "category" in failed
                            or any(c.verdict == "unknown" for c in hard_checks)
                        ):
                            raise ValueError(
                                "alternative must differ in exactly one known dimension"
                            )
                if any(c.sku_id not in product.candidate_sku_ids for c in product.condition_checks):
                    raise ValueError("condition checks must belong to returned candidate versions")
            ids = [sku for p in self.products for sku in p.candidate_sku_ids]
            if len(ids) != len(set(ids)) or len(ids) > self.candidate_sku_count:
                raise ValueError("returned versions must be unique and within the match count")
            total = self.candidate_count
        if self.selection_scope is None or self.offset is None or self.limit is None:
            raise ValueError("executed result requires granularity and its applied window")
        if self.requested_count is not None:
            if self.limit != min(self.requested_count, self.display_cap):
                raise ValueError("applied limit must respect requested count and display cap")
            if self.limit_capped != (self.requested_count > self.display_cap):
                raise ValueError("count truncation must reflect the display cap")
        expected_count = min(self.limit, max(0, total - self.offset))
        budget_window = bool(self.output_budget and self.output_budget.truncated
                             and "products" in self.output_budget.omitted_sections)
        if (self.returned_count > expected_count or
                (self.returned_count != expected_count and not budget_window)):
            raise ValueError("page size must agree with offset, limit and total")
        expected_next = self.offset + self.returned_count
        if self.next_offset != (expected_next if expected_next < total else None):
            raise ValueError("next offset must describe the next available page")
        return self
