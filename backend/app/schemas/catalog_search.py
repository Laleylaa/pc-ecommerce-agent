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

from app.core.llm_feedback import OutputValidationError
from app.schemas.catalog import ProductCard
from app.tools.catalog_fields import hard_condition_verdict, normalize_category_hint
from app.tools.catalog_search.outcomes import (
    LookupStatus,
    QueryState,
    ResultAction,
    ResultReason,
    ResultStatus,
    completed_policy,
    validate_policy_fields,
)
from app.tools.catalog_search.outcomes import (
    data_quality as assess_data_quality,
)

QueryText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000)
]
PositiveId = Annotated[int, Field(strict=True, gt=0)]
Count = Annotated[int, Field(strict=True, ge=0)]
CategoryHint = Annotated[
    Annotated[str, StringConstraints(strict=True, strip_whitespace=True, max_length=40)] | None,
    AfterValidator(normalize_category_hint),
    Field(
        description="可选品类提示：鼠标/键盘/耳机/显示器/音箱/摄像头；仅选择规格说明，不是筛选条件。未知或空值使用统一说明。"
    ),
]


class SearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_ids: list[PositiveId] = Field(default_factory=list, max_length=50)
    spu_ids: list[PositiveId] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_ids(self) -> "SearchScope":
        if any(value <= 0 for value in self.sku_ids + self.spu_ids):
            raise ValueError("scope identifiers must be positive")
        if self.sku_ids and self.spu_ids:
            raise OutputValidationError("use one scope type: sku_ids or spu_ids")
        self.sku_ids = list(dict.fromkeys(self.sku_ids))
        self.spu_ids = list(dict.fromkeys(self.spu_ids))
        return self


class CatalogSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: QueryText = Field(description="完整的单目标商品查询")
    category_hint: CategoryHint = None
    scope: SearchScope = Field(default_factory=SearchScope)
    limit: int = Field(
        default=3,
        ge=1,
        le=20,
        strict=True,
        description=(
            "返回候选数量上限。用户明确要求N款时必须填写N，仅在query写数量不能替代此参数；"
            "仅未指定数量时省略，默认最多3款。"
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
        "key", "operator", "values", "evidence", "strength", "preference_evidence",
    )

    model_config = ConfigDict(extra="forbid")

    key: str
    operator: Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte"] = Field(
        default="eq",
        description="以上必须gte（含本数），以下必须lte（含本数）；gt仅超过，lt仅低于；不要把以上写成gt",
    )
    values: list[str] = Field(min_length=1, max_length=12)
    evidence: str = Field(min_length=1, description="原始查询中表达该条件的原文")
    strength: Literal["requirement", "preference"] = "requirement"
    preference_evidence: str | None = Field(
        default=None, description="仅preference填写原文中明确可让步的表达；普通规格默认requirement"
    )

    @model_validator(mode="after")
    def validate_arity(self) -> "SearchCondition":
        if self.operator not in {"in", "not_in"} and len(self.values) != 1:
            raise OutputValidationError("this operator takes exactly one value", path=("values",))
        if (self.strength == "preference") != bool(self.preference_evidence):
            raise OutputValidationError(
                "preferences require evidence; requirements must not have it",
                path=("preference_evidence",),
            )
        return self


class UnresolvedCatalogRequirement(BaseModel):
    """A preserved user requirement with an explicit reason it cannot execute."""

    model_config = ConfigDict(extra="forbid")
    evidence: QueryText
    reason: Literal[
        "filter_data_unavailable", "requirement_needs_clarification", "request_not_supported",
    ]


class SearchPlan(BaseModel):
    # Model output must declare decisions; deterministic constructors retain defaults.
    model_required_fields: ClassVar[tuple[str, ...]] = (
        "operation", "disposition", "category", "name", "target", "keywords",
        "conditions", "requested_fields", "unsupported_requirements", "clarification_requirements",
        "sort", "purpose",
    )

    model_config = ConfigDict(extra="forbid")

    operation: Literal["search", "lookup", "variants"] = "search"
    disposition: Literal["ready", "split_required", "clarification_required", "unsupported"] = (
        "ready"
    )
    reason: str = ""
    category: str | None = Field(
        default=None,
        description="只取query明说的品类；名称查询通常null，不能从型号、面板、声道、功率或hint推断",
    )
    category_evidence: str | None = Field(
        default=None, description="用户明确品类的原文，未说则null"
    )
    name: str | None = Field(
        default=None,
        description="具体型号/版本及其标题限定词；原文片段用空格连接，保留品牌，每段须来自query；泛标题类型词用keywords，不翻译品牌",
    )
    keywords: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="无具体名称时的标题包含词，如机械键盘；有name时标题限定并入name，不重复规格条件或把用途转成关键词",
    )
    target: ProductReference | None = Field(
        default=None,
        description="仅用于query中明确写出的SKU/SPU编号；可与同一对象的name共同限定身份，不要复制scope编号"
    )
    conditions: list[SearchCondition] = Field(default_factory=list, max_length=24)
    unresolved_requirements: list[UnresolvedCatalogRequirement] = Field(
        default_factory=list, max_length=12,
        description="不能执行的要求及明确根因；逐字引用原文，不与conditions或旧声明列表重复",
    )
    requested_fields: list[str] = Field(default_factory=list, max_length=24)
    unsupported_requirements: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="仅无法表达为conditions的具体筛选要求，逐字引用；不重复conditions或clarification_requirements。对象事实询问只放requested_fields",
    )
    clarification_requirements: list[QueryText] = Field(
        default_factory=list,
        max_length=12,
        description="需要具体化为参数的用途/主观筛选要求，逐字引用query；能力缺失另记unsupported_requirements",
    )
    unsupported_reason: Literal[
        ResultReason.REQUEST_NOT_SUPPORTED, ResultReason.DATA_INSUFFICIENT,
        ResultReason.FILTER_DATA_UNAVAILABLE,
    ] = ResultReason.REQUEST_NOT_SUPPORTED
    sort: Literal["relevance", "price_asc", "price_desc", "sales", "stock"] = "relevance"
    sort_evidence: str | None = Field(default=None, description="非默认排序依据的用户原文")
    purpose: Literal["search", "lookup", "recommendation"] = "search"
    requested_limit: int | None = Field(default=None, ge=1, le=10000)


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
    candidate_sku_ids: list[int] = Field(default_factory=list)
    model_sku_count: Count = 0
    model_min_price: Decimal | None = None
    model_max_price: Decimal | None = None
    field_issues: list[FieldIssue] = Field(default_factory=list)

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
            if self.series_sku_count != len(ids):
                raise ValueError("series count must match the variant list")
            if self.series_total_stock != sum(v.stock for v in variants):
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
            if (self.series_min_price, self.series_max_price) != (
                min(v.price for v in variants),
                max(v.price for v in variants),
            ):
                raise ValueError("series prices must describe the matching variants")
        if self.model_sku_count < len(ids):
            raise ValueError("model count cannot be smaller than its matching subset")
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


class CatalogSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["3"] = "3"
    match_kind: Literal["exact", "alternative", "object_facts", "none"] = "none"
    fallback_attempted: bool = False
    identity_match_count: Count | None = None
    candidate_sku_count: Count | None = None
    candidate_spu_count: Count | None = None
    result_type: Literal["products", "empty"]
    status: ResultStatus
    reason_code: ResultReason
    recommended_action: ResultAction
    retryable: bool = False
    query_state: QueryState = "not_executed"
    result_purpose: Literal["search", "lookup", "recommendation"] = "search"
    selection_scope: Literal["sku", "spu"] | None = None
    products: list[SearchProduct] = Field(default_factory=list)
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
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] | None = None
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
        if self.returned_count != len(self.products):
            raise ValueError("returned count must match the returned collection")
        kind = "products" if self.products else "empty"
        if self.result_type != kind:
            raise ValueError("products and identity candidates are distinct result types")
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
            if any(value is not None for value in counts) or self.products:
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
            if self.matching_complete != (self.unknown_sku_count == 0):
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
                object_status=self.object_results[0].status if self.object_results else None,
                named_target_missing=(
                    self.reason_code == ResultReason.TARGET_NOT_FOUND
                    and bool(self.query_plan.get("name"))
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
                conditions = [
                    SearchCondition.model_validate(c) for c in self.query_plan.get("conditions", [])
                ]
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
        if self.returned_count != min(self.limit, max(0, total - self.offset)):
            raise ValueError("page size must agree with offset, limit and total")
        expected_next = self.offset + self.returned_count
        if self.next_offset != (expected_next if expected_next < total else None):
            raise ValueError("next offset must describe the next available page")
        return self
