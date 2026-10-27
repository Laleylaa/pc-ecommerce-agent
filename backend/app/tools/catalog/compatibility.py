"""Retained legacy search and rule-based adapters; formal LLM facets live separately."""
import asyncio
import json
import re
from decimal import Decimal
from typing import Any, Literal, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.llm_feedback import OutputErrorType, OutputValidationError, validation_feedback
from app.models import Brand, Category, Sku, Spu
from app.repositories.catalog import (
    CATEGORY_ALIASES,
    CatalogRepository,
    _matches_single_filter,
)
from app.schemas.catalog import (
    ProductCard,
    ProductSearchRequest,
    ProductSpecCondition,
    ProductVariantCard,
)
from app.tools.catalog.context import (
    _catalog_context_summary,
    _catalog_query_references_current_product,
    _resolved_catalog_request_context,
)
from app.tools.catalog.facets import CatalogFacetService
from app.tools.catalog.facets_plan import FacetQueryPlan, _validate_catalog_facet_plan
from app.tools.catalog.facets_planner import CatalogFacetPlanner
from app.tools.catalog.fields import (
    CATEGORY_FIELDS,
    canonical_field,
)
from app.tools.catalog.normalization import (
    ALLOWED_CATEGORIES,
    ALLOWED_FILTERS,
    _canonical_category,
    _normalize_catalog_filters_with_debug,
    _normalize_filter_key,
)
from app.tools.prompts.templates import load_prompt
from app.tools.schemas import (
    CatalogFacetInput,
    CatalogSearchInput,
    CatalogSearchOutput,
    CatalogSeriesSpecSummary,
    CatalogSeriesSpecValue,
    CatalogSeriesSummary,
    CatalogSeriesVariant,
    ToolDiagnostic,
)

CATALOG_ALLOWED_TABLES = {
    "brand",
    "category",
    "spu",
    "sku",
    "goods_attribute_relation",
    "attribute_key",
    "attribute_value",
}

CATEGORY_FILTERS = {
    alias: set(CATEGORY_FIELDS[category].split())
    for alias, category in CATEGORY_ALIASES.items()
    if category in CATEGORY_FIELDS
}
ALLOWED_USAGE_SCENARIOS = {
    "office",
    "gaming",
    "video_meeting",
    "live_streaming",
}
USAGE_SCENARIO_ALIASES = {
    "office": "office",
    "办公": "office",
    "工作": "office",
    "学习": "office",
    "gaming": "gaming",
    "game": "gaming",
    "fps": "gaming",
    "esports": "gaming",
    "游戏": "gaming",
    "电竞": "gaming",
    "video_meeting": "video_meeting",
    "video meeting": "video_meeting",
    "video conference": "video_meeting",
    "video conferencing": "video_meeting",
    "视频会议": "video_meeting",
    "远程会议": "video_meeting",
    "网课": "video_meeting",
    "开会": "video_meeting",
    "live_streaming": "live_streaming",
    "live streaming": "live_streaming",
    "livestream": "live_streaming",
    "直播": "live_streaming",
    "主播": "live_streaming",
    "开播": "live_streaming",
}
USAGE_MAPPING_VERSION = "v1"
USAGE_SCENARIO_CATEGORIES = {
    "office": ("keyboard", "monitor", "headset", "webcam"),
    "gaming": ("mouse", "keyboard", "headset", "monitor", "speaker"),
    "video_meeting": ("webcam", "headset"),
    "live_streaming": ("webcam",),
}
USAGE_CATEGORY_CONCURRENCY = 3


def _condition(
    key: str,
    operator: Literal["exact", "eq", "in", "gte", "lte"],
    *values: str,
) -> ProductSpecCondition:
    return ProductSpecCondition(key=key, operator=operator, values=list(values))


USAGE_SCENARIO_RULES: dict[tuple[str, str], dict[str, tuple[ProductSpecCondition, ...]]] = {
    ("office", "keyboard"): {
        "preferred": (_condition("switches", "exact", "静音红轴"),),
    },
    ("office", "monitor"): {
        "preferred": (
            _condition("panel_type", "eq", "IPS"),
            _condition("resolution", "eq", "2560x1440"),
            _condition("size_inch", "eq", "27"),
        ),
    },
    ("office", "headset"): {
        "preferred": (
            _condition("microphone", "eq", "是"),
            _condition("enclosure_type", "eq", "封闭式"),
        ),
    },
    ("office", "webcam"): {
        "required": (_condition("microphone", "eq", "是"),),
        "preferred": (_condition("frame_rate", "gte", "60"),),
    },
    ("gaming", "mouse"): {
        "preferred": (
            _condition("weight_g", "lte", "65"),
            _condition("max_dpi", "gte", "16000"),
        ),
    },
    ("gaming", "keyboard"): {
        "preferred": (_condition("switches", "in", "磁轴", "线性红轴"),),
    },
    ("gaming", "headset"): {
        "preferred": (
            _condition("microphone", "eq", "是"),
            _condition("enclosure_type", "eq", "封闭式"),
        ),
    },
    ("gaming", "monitor"): {
        "preferred": (
            _condition("refresh_rate", "gte", "144"),
            _condition("response_time_ms", "lte", "1"),
        ),
    },
    ("gaming", "speaker"): {
        "preferred": (_condition("channels", "in", "2.1", "5.1"),),
    },
    ("video_meeting", "webcam"): {
        "required": (_condition("microphone", "eq", "是"),),
        "preferred": (_condition("frame_rate", "gte", "60"),),
    },
    ("video_meeting", "headset"): {
        "required": (_condition("microphone", "eq", "是"),),
        "preferred": (_condition("enclosure_type", "eq", "封闭式"),),
    },
    ("live_streaming", "webcam"): {
        "required": (
            _condition("resolution", "in", "1080p HDR", "1440p", "4K"),
            _condition("frame_rate", "in", "60fps", "90fps"),
        ),
        "preferred": (_condition("field_of_view", "in", "90°", "103°"),),
    },
}
ALLOWED_SORTS = {"recommend", "sales", "price_asc", "price_desc", "stock"}
CATALOG_SEARCH_FIELDS = {
    "product_identity",
    "title",
    "brand",
    "category",
    "price",
    "stock",
    "availability",
    "sku_sales_count",
    "sales_count",
    "specifications",
    "variants",
    "series_price_range",
    "series_stock",
    "series_options",
    *ALLOWED_FILTERS,
}
UNSUPPORTED_QUERY_PATTERNS = {
    "growth": "current catalog data has no time-series sales history",
    "增长": "current catalog data has no time-series sales history",
    "month over month": "current catalog data has no time-series sales history",
    "revenue": "current catalog tool does not support revenue analytics",
    "profit": "current catalog tool does not support profit analytics",
    "利润": "current catalog tool does not support profit analytics",
    "用户": "catalog tool cannot query user purchase statistics",
    "购买过": "catalog tool cannot query user purchase statistics",
}


class ProductQueryPlan(BaseModel):
    query: str = Field(min_length=1)
    category: str | None = None
    brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_brands: list[str] = Field(default_factory=list, max_length=8)
    excluded_usage: list[str] = Field(default_factory=list, max_length=8)
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    requested_fields: list[str] = Field(default_factory=list, max_length=24)
    usage_scenario: str | None = None
    usage_mapping: dict = Field(default_factory=dict)
    sort: Literal["recommend", "sales", "price_asc", "price_desc", "stock"] = "recommend"
    result_purpose: Literal[
        "recommendation",
        "search",
        "lookup",
        "ranking",
        "count",
    ] = "search"
    selection_scope: Literal["sku", "spu"] = "spu"
    limit: int = Field(default=3, ge=1, le=20)
    supported: bool = True
    unsupported_reason: str | None = None
    planner: str = "rule_based"
    fallback_reason: str | None = None
    normalization_debug: dict = Field(default_factory=dict)
    ranking: "CatalogRankingPlan | None" = None

    @model_validator(mode="after")
    def validate_price_range(self) -> "ProductQueryPlan":
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise OutputValidationError(
                "min_price cannot be greater than max_price", path=("min_price",),
            )
        return self


class CatalogRankingPlan(BaseModel):
    scope: Literal["sku", "spu"]
    metric: Literal["price", "stock", "sales"]
    direction: Literal["asc", "desc"]
    rank: int = Field(default=1, ge=1, le=20)
    count: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="after")
    def validate_window(self) -> "CatalogRankingPlan":
        if self.rank + self.count - 1 > 20:
            raise OutputValidationError("catalog ranking window cannot exceed 20", path=("rank",))
        return self


class CatalogQueryPlanner(Protocol):
    async def plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan: ...


    async def plan_facets(self, request: CatalogFacetInput) -> "FacetQueryPlan": ...














class RuleBasedCatalogQueryPlanner:
    """Default offline planner. An LLM/NL2SQL planner can replace this interface later."""

    async def plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan:
        # Legacy search uses its existing tool-owned rules, not agent intent parsing.
        parsed = ProductSearchRequest(
            query=request.query, category=_infer_category_from_text(request.query),
            max_price=_max_price_from_query(request.query),
            filters=_filters_from_query(request.query), limit=request.limit,
        )
        defaults = request.preference_defaults
        default_filters = (
            {"connection_type": defaults.connection_type}
            if defaults.connection_type is not None
            else {}
        )
        inferred_filters = _filters_from_query(request.query)
        inferred_max_price = _max_price_from_query(request.query)
        filters = {**default_filters, **inferred_filters, **parsed.filters, **request.filters}
        min_price = request.min_price if request.min_price is not None else parsed.min_price
        max_price = (
            request.max_price
            if request.max_price is not None
            else parsed.max_price
            if parsed.max_price is not None
            else inferred_max_price
            if inferred_max_price is not None
            else defaults.max_price
        )
        unsupported_reason = _unsupported_reason(request.query)
        explicit_brands = request.brands or ([request.brand] if request.brand else [])
        excluded_brands = _dedupe_keep_order([*defaults.excluded_brands, *request.excluded_brands])
        requested_exclusions = {item.lower() for item in request.excluded_brands}
        inferred_brands = _infer_brands_from_text(request.query)
        selected_brands = explicit_brands or inferred_brands or defaults.brands
        selected_brands = [
            brand for brand in selected_brands if brand.lower() not in requested_exclusions
        ]
        excluded_brands = [
            brand
            for brand in excluded_brands
            if brand.lower() in requested_exclusions
            or brand.lower() not in {item.lower() for item in explicit_brands}
        ]
        excluded_usage = _dedupe_keep_order([*defaults.excluded_usage, *request.excluded_usage])
        inferred_usage = _usage_from_query(request.query)
        if inferred_usage in request.excluded_usage:
            inferred_usage = None
        usage = request.usage or inferred_usage
        if usage is None and defaults.usage not in excluded_usage:
            usage = defaults.usage
        if request.usage:
            excluded_usage = [item for item in excluded_usage if item != usage]
        ranking = _ranking_plan_from_query(
            request.query,
            request.limit,
            has_context_target=bool(request.targets),
        )
        result_purpose = _catalog_result_purpose_from_query(
            request.query,
            has_context_target=bool(request.targets),
            has_ranking=ranking is not None,
        )
        selection_scope = _resolved_catalog_selection_scope(
            request.query,
            has_context_target=bool(request.targets),
            planned_scope=ranking.scope if ranking is not None else None,
            result_purpose=result_purpose,
            filters=filters,
            has_price_constraint=min_price is not None or max_price is not None,
        )
        if ranking is not None:
            ranking = ranking.model_copy(update={"scope": selection_scope})
        sort = _sort_from_query(request.query) or request.sort
        if result_purpose == "recommendation" and sort == "recommend":
            sort = "sales"
        return ProductQueryPlan(
            query=request.query,
            category=(
                request.category
                or parsed.category
                or _infer_category_from_text(request.query.lower())
            ),
            brands=selected_brands,
            excluded_brands=excluded_brands,
            excluded_usage=excluded_usage,
            min_price=min_price,
            max_price=max_price,
            filters=filters,
            keywords=_dedupe_keep_order([*request.keywords, *([usage] if usage else [])]),
            requested_fields=_deterministic_catalog_search_fields(request.query),
            usage_scenario=usage,
            sort=sort,
            result_purpose=result_purpose,
            selection_scope=selection_scope,
            limit=ranking.count if ranking is not None else request.limit,
            supported=unsupported_reason is None,
            unsupported_reason=unsupported_reason,
            ranking=ranking,
        )


    async def plan_facets(self, request: CatalogFacetInput) -> FacetQueryPlan:
        if not request.query.strip():
            return _facet_query_plan(request)
        # A fully consumed grammar maps its captured dimension through the same
        # field aliases as validation/execution. No substring intent extraction.
        match = re.fullmatch(
            r"\s*(?:(?:本店|全店|店里|店铺|商城)(?:有|卖|销售)?)?"
            r"(?P<category>鼠标|键盘|耳机|显示器|音箱|摄像头)?(?:有|支持)?"
            r"(?:有哪些|有什么|哪些|什么)(?P<dimension>品牌|牌子|品类|类别|规格维度|规格|"
            r"颜色|配列|布局|轴体|连接方式)(?:可选|选项)?[？?。]?\s*",
            request.query,
        )
        if match is None:
            raise OSError("Natural-language catalog facet planning requires a model")
        dimension = match["dimension"]
        facet = (
            "brand" if dimension in {"品牌", "牌子"}
            else "category" if dimension in {"品类", "类别"}
            else "spec_key" if dimension in {"规格维度", "规格"}
            else "spec_value"
        )
        plan = FacetQueryPlan(
            query=request.query, facet=facet,
            category=match["category"] or request.category,
            brand=request.brand,
            spec_key=canonical_field(dimension) if facet == "spec_value" else None,
            filters=request.filters, min_price=request.min_price, max_price=request.max_price,
            limit=request.limit,
        )
        return _validate_catalog_facet_plan(plan)


class LLMCatalogQueryPlanner(CatalogFacetPlanner):
    """LLM planner that returns a guarded ProductQueryPlan JSON, not raw SQL."""

    def __init__(self, chat_model, *, review: bool = True):
        self.chat_model = chat_model
        self.review = review

    async def plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan:
        explicit_brands = request.brands or ([request.brand] if request.brand else [])
        return await self._plan(
            task="search",
            query=request.query,
            limit=request.limit,
            overrides={
                "category": request.category,
                "brands": explicit_brands,
                "excluded_brands": request.excluded_brands,
                "excluded_usage": request.excluded_usage,
                "min_price": request.min_price,
                "max_price": request.max_price,
                "filters": request.filters,
                "keywords": request.keywords,
                "usage_scenario": request.usage,
                "sort": request.sort if request.sort != "recommend" else None,
            },
            preference_defaults=request.preference_defaults.model_dump(mode="json"),
            catalog_context=_catalog_context_summary(request),
        )





    async def _plan(
        self,
        task: str,
        query: str,
        limit: int,
        overrides: dict,
        preference_defaults: dict,
        catalog_context: dict,
    ) -> ProductQueryPlan:
        payload = {
            "task": task,
            "query": query,
            "limit": limit,
            "explicit_overrides": _json_safe(overrides),
            "preference_defaults": _json_safe(preference_defaults),
            "catalog_context": _json_safe(catalog_context),
        }
        plan_data = await self._invoke_with_retry(
            system_prompt=_catalog_planner_system_prompt(),
            payload=payload,
            validator=_validate_product_plan_data,
        )
        plan_data["query"] = query
        plan_data["limit"] = limit
        _coerce_filter_values_to_strings(plan_data)
        plan = ProductQueryPlan.model_validate(plan_data)
        plan.planner = "llm"
        _apply_high_confidence_search_overrides(plan, query)
        _apply_preference_defaults(plan, preference_defaults)
        _apply_explicit_overrides(plan, overrides)
        return plan

    async def _invoke_with_retry(self, system_prompt: str, payload: dict, validator) -> dict:
        last_error: ValidationError | OutputValidationError | None = None
        retry_payload = dict(payload)
        for _attempt in range(2):
            response = await self.chat_model.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=json.dumps(retry_payload, ensure_ascii=False)),
                ]
            )
            raw_output = _message_content_to_text(response.content)
            plan_data: dict | None = None
            try:
                plan_data = _extract_json_object(raw_output)
            except (json.JSONDecodeError, OutputValidationError) as exc:
                if isinstance(exc, json.JSONDecodeError):
                    exc = OutputValidationError(
                        str(exc), error_type=OutputErrorType.FORMAT_ERROR, code="invalid_json",
                    )
                last_error = exc
            else:
                try:
                    validator(plan_data)
                except (ValidationError, OutputValidationError) as exc:
                    last_error = exc
                else:
                    return plan_data
            assert last_error is not None
            feedback = _retry_feedback(plan_data, last_error, raw_output)
            if _attempt == 0:
                retry_payload = {
                    **payload,
                    "validation_feedback": feedback,
                    "retry_instruction": load_prompt("catalog/compatibility_repair.md"),
                }
        raise ValueError(f"LLM planner failed validation after retry: {last_error}") from last_error


class CatalogToolService(CatalogFacetService):
    def __init__(
        self,
        session: AsyncSession,
        planner: CatalogQueryPlanner | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ):
        self.session = session
        self.planner = planner or RuleBasedCatalogQueryPlanner()
        self.session_factory = session_factory

    async def search(self, request: CatalogSearchInput) -> CatalogSearchOutput:
        context_targets, _context_scope = _resolved_catalog_request_context(
            request.query,
            request.catalog_context,
        )
        if not request.targets and context_targets:
            request = request.model_copy(update={"targets": context_targets})
        requested_limit = _requested_product_count(request.query)
        if requested_limit is not None:
            request = request.model_copy(update={"limit": requested_limit})
        plan = await self._safe_plan_search(request)
        result_purpose = plan.result_purpose
        selection_scope = plan.selection_scope
        resolved_sort = plan.sort
        if result_purpose == "recommendation" and resolved_sort == "recommend":
            resolved_sort = "sales"
        update: dict[str, Any] = {
            "selection_scope": selection_scope,
            "result_purpose": result_purpose,
            "sort": resolved_sort,
        }
        if plan.ranking is not None:
            update["ranking"] = plan.ranking.model_copy(
                update={"scope": selection_scope}
            )
        plan = plan.model_copy(update=update)
        if plan.supported and plan.usage_scenario and not plan.category:
            return await self._search_usage_across_categories(request, plan)
        plan = _apply_usage_scenario_mapping(plan)
        if not plan.supported:
            diagnostics = _catalog_plan_diagnostics(plan, result_type="empty", count=0)
            return CatalogSearchOutput(
                result_type="empty",
                result_purpose=plan.result_purpose,
                selection_scope=plan.selection_scope,
                products=[],
                ranking_strategy="unsupported_query",
                query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
                diagnostics=diagnostics,
            )

        product_request = _plan_to_product_search(plan)
        context_spu_ids = (
            await self._resolved_target_ids(request, "spu")
            if request.targets
            else []
        )
        materialize_context_with_ranking = bool(
            context_spu_ids
            and plan.ranking is not None
            and _catalog_query_references_current_product(request.query)
        )
        if request.targets and not context_spu_ids:
            diagnostics = [
                ToolDiagnostic(
                    code="invalid_context_target",
                    severity="error",
                    message="The referenced catalog product is unavailable.",
                    recommended_action="ask_user_to_select_an_active_product",
                )
            ]
            return CatalogSearchOutput(
                result_type="empty",
                result_purpose=plan.result_purpose,
                selection_scope=plan.selection_scope,
                products=[],
                ranking_strategy="invalid_context_target",
                query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
                diagnostics=diagnostics,
            )
        if context_spu_ids and not materialize_context_with_ranking:
            product_request = product_request.model_copy(
                update={
                    "spu_ids": context_spu_ids,
                    # The trusted identity already fixes the series. Deictic wording such
                    # as "这个系列" is selection semantics, not a product-title keyword.
                    "query": "",
                }
            )
        repository = CatalogRepository(self.session)
        deduplicate_recommendation_spus = (
            plan.selection_scope == "sku"
            and not context_spu_ids
            and (
                plan.result_purpose == "recommendation"
                or _catalog_query_requests_recommendation(request.query)
            )
        )
        if plan.ranking is not None:
            ranking = plan.ranking
            if ranking.scope == "spu":
                page = await repository.search_product_series_by_ranking_with_total(
                    product_request,
                    metric=ranking.metric,
                    direction=ranking.direction,
                    rank=ranking.rank,
                    count=ranking.count,
                )
            else:
                page = await repository.search_skus_by_ranking_with_total(
                    product_request,
                    metric=ranking.metric,
                    direction=ranking.direction,
                    rank=ranking.rank,
                    count=ranking.count,
                    distinct_spu=deduplicate_recommendation_spus,
                )
                products = [
                    _with_sku_ranking(product, ranking)
                    for product in page.products
                ]
            ranking_strategy = (
                f"{ranking.scope}_{ranking.metric}_{ranking.direction}"
            )
        elif plan.selection_scope == "spu":
            page = await repository.search_product_series_with_total(product_request)
            ranking_strategy = "spu_match_score_sales_stock_price"
        else:
            page = await repository.search_products_with_total(
                product_request,
                distinct_spu=deduplicate_recommendation_spus,
            )
            ranking_strategy = (
                "match_score_sales_stock_price" if plan.sort == "recommend" else plan.sort
            )
        if plan.ranking is None or plan.ranking.scope == "spu":
            products = page.products
        total_match_count = page.total_count
        if (
            not products
            and plan.ranking is None
            and not context_spu_ids
            and len(plan.filters) >= 2
            and _catalog_query_requests_near_match(request.query)
        ):
            near_match_result = await self._search_near_matches(
                request,
                plan,
                repository,
            )
            if near_match_result is not None:
                return near_match_result
        if materialize_context_with_ranking:
            context_products = await self._context_target_products(
                request,
                plan.selection_scope,
            )
            products = _merge_catalog_product_sets(
                context_products,
                products,
                scope=plan.selection_scope,
            )
            total_match_count = max(total_match_count, len(products))
        if plan.selection_scope == "spu" and products:
            products = await self._enrich_series_products(products)
        products = _filter_brands(products, plan.brands)
        products = _filter_excluded_preferences(products, plan.excluded_brands, plan.excluded_usage)
        result_type = "products" if products else "empty"
        diagnostics = _catalog_plan_diagnostics(plan, result_type=result_type, count=len(products))
        result_limit = (
            max(2, plan.ranking.count + len(context_spu_ids))
            if materialize_context_with_ranking and plan.ranking is not None
            else plan.ranking.count if plan.ranking else request.limit
        )
        returned_products = products[:result_limit]
        return CatalogSearchOutput(
            result_type=result_type,
            result_purpose=plan.result_purpose,
            selection_scope=plan.selection_scope,
            products=returned_products,
            total_match_count=total_match_count,
            returned_count=len(returned_products),
            is_exhaustive=len(returned_products) >= total_match_count,
            ranking_strategy=ranking_strategy,
            query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
            diagnostics=diagnostics,
        )

    async def _search_near_matches(
        self,
        request: CatalogSearchInput,
        plan: ProductQueryPlan,
        repository: CatalogRepository,
    ) -> CatalogSearchOutput | None:
        """Return candidates that miss exactly one explicit filter.

        This fallback is only reached for an explicit near-match request after the
        exact query returned no rows. Category, brand, price, exclusions, and every
        non-relaxed filter remain hard constraints.
        """
        requested_filters = dict(plan.filters)
        candidate_products: dict[int, ProductCard] = {}
        candidate_differences: dict[int, dict[str, dict[str, str | None]]] = {}
        pool_limit = min(20, max(12, request.limit * 4))

        for relaxed_key in requested_filters:
            retained_filters = {
                key: value
                for key, value in requested_filters.items()
                if key != relaxed_key
            }
            relaxed_plan = plan.model_copy(
                update={
                    "filters": retained_filters,
                    "selection_scope": "sku",
                    "limit": pool_limit,
                }
            )
            relaxed_request = _plan_to_product_search(relaxed_plan).model_copy(
                update={"limit": pool_limit}
            )
            page = await repository.search_products_with_total(
                relaxed_request,
                distinct_spu=False,
            )
            for product in page.products:
                missing_filters = _catalog_filter_differences(
                    product,
                    requested_filters,
                )
                if len(missing_filters) != 1:
                    continue
                candidate_products[product.sku_id] = product
                candidate_differences[product.sku_id] = missing_filters

        candidates = _filter_brands(list(candidate_products.values()), plan.brands)
        candidates = _filter_excluded_preferences(
            candidates,
            plan.excluded_brands,
            plan.excluded_usage,
        )
        if not candidates:
            return None

        returned_products = _select_balanced_near_matches(
            candidates,
            candidate_differences,
            request.limit,
        )
        returned_ids = {product.sku_id for product in returned_products}
        returned_differences = {
            str(sku_id): differences
            for sku_id, differences in candidate_differences.items()
            if sku_id in returned_ids
        }
        diagnostics = [
            ToolDiagnostic(
                code="ok",
                severity="info",
                message=(
                    "The exact catalog query was empty; explicit near-match retrieval "
                    "returned candidates that each differ by one filter."
                ),
                recommended_action="explain_each_relaxed_filter",
                details={
                    "match_mode": "single_filter_relaxation",
                    "requested_filters": requested_filters,
                },
            )
        ]
        query_plan = _plan_dump_with_diagnostics(
            plan.model_copy(
                update={
                    "selection_scope": "sku",
                    "sort": "recommend",
                }
            ),
            diagnostics,
        )
        query_plan["relaxation"] = {
            "applied": True,
            "mode": "single_filter",
            "selection_strategy": "balanced_across_relaxed_filters",
            "within_dimension_sort": "sales",
            "exact_match_count": 0,
            "requested_filters": requested_filters,
            "candidate_differences": returned_differences,
        }
        return CatalogSearchOutput(
            result_type="products",
            result_purpose=plan.result_purpose,
            selection_scope="sku",
            products=returned_products,
            total_match_count=len(candidates),
            returned_count=len(returned_products),
            is_exhaustive=len(returned_products) >= len(candidates),
            ranking_strategy="near_match_single_filter_balanced",
            query_plan=query_plan,
            diagnostics=diagnostics,
        )

    async def _search_usage_across_categories(
        self,
        request: CatalogSearchInput,
        plan: ProductQueryPlan,
    ) -> CatalogSearchOutput:
        categories = USAGE_SCENARIO_CATEGORIES.get(plan.usage_scenario or "", ())
        if not categories:
            plan = _mark_usage_mapping_unavailable(plan)
            diagnostics = _catalog_plan_diagnostics(plan, result_type="empty", count=0)
            return CatalogSearchOutput(
                result_type="empty",
                result_purpose=plan.result_purpose,
                selection_scope=plan.selection_scope,
                products=[],
                ranking_strategy="unsupported_query",
                query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
                diagnostics=diagnostics,
            )

        per_category_limit = min(20, max(2, request.limit))
        semaphore = asyncio.Semaphore(USAGE_CATEGORY_CONCURRENCY)

        async def search_category(
            category: str,
        ) -> tuple[str, ProductQueryPlan, list[ProductCard], int]:
            category_plan = plan.model_copy(
                deep=True,
                update={"category": category, "limit": per_category_limit, "usage_mapping": {}},
            )
            category_plan = _apply_usage_scenario_mapping(category_plan)
            if not category_plan.supported:
                return category, category_plan, [], 0

            async with semaphore:
                if self.session_factory is None:
                    repository = CatalogRepository(self.session)
                    search_request = _plan_to_product_search(category_plan)
                    page = (
                        await repository.search_product_series_with_total(search_request)
                        if category_plan.selection_scope == "spu"
                        else await repository.search_products_with_total(
                            search_request,
                            distinct_spu=(
                                category_plan.result_purpose == "recommendation"
                                or _catalog_query_requests_recommendation(request.query)
                            ),
                        )
                    )
                else:
                    async with self.session_factory() as category_session:
                        repository = CatalogRepository(category_session)
                        search_request = _plan_to_product_search(category_plan)
                        page = (
                            await repository.search_product_series_with_total(search_request)
                            if category_plan.selection_scope == "spu"
                            else await repository.search_products_with_total(
                                search_request,
                                distinct_spu=(
                                    category_plan.result_purpose == "recommendation"
                                    or _catalog_query_requests_recommendation(request.query)
                                ),
                            )
                        )
            products = page.products
            products = _filter_brands(products, category_plan.brands)
            products = _filter_excluded_preferences(
                products,
                category_plan.excluded_brands,
                category_plan.excluded_usage,
            )
            return category, category_plan, products[:per_category_limit], page.total_count

        if self.session_factory is None:
            category_results = []
            for category in categories:
                category_results.append(await search_category(category))
            execution = "sequential_shared_request_session"
        else:
            category_results = await asyncio.gather(
                *(search_category(category) for category in categories)
            )
            execution = "parallel_independent_sessions"

        products = _round_robin_category_products(category_results, limit=request.limit)
        total_match_count = sum(total_count for _, _, _, total_count in category_results)
        if plan.selection_scope == "spu" and products:
            products = await self._enrich_series_products(products)
        plan.usage_mapping = {
            "status": "expanded",
            "source": "deterministic_spec_mapping",
            "rule_version": USAGE_MAPPING_VERSION,
            "scenario": plan.usage_scenario,
            "category": None,
            "categories": list(categories),
            "execution": execution,
            "category_rules": {
                category: category_plan.usage_mapping
                for category, category_plan, _products, _total_count in category_results
            },
        }
        result_type = "products" if products else "empty"
        diagnostics = _catalog_plan_diagnostics(
            plan,
            result_type=result_type,
            count=len(products),
        )
        return CatalogSearchOutput(
            result_type=result_type,
            result_purpose=plan.result_purpose,
            selection_scope=plan.selection_scope,
            products=products,
            total_match_count=total_match_count,
            returned_count=len(products),
            is_exhaustive=len(products) >= total_match_count,
            ranking_strategy="scenario_category_diversified_mapping",
            query_plan=_plan_dump_with_diagnostics(plan, diagnostics),
            diagnostics=diagnostics,
        )



    async def _resolved_target_ids(
        self,
        request: CatalogSearchInput,
        level: Literal["sku", "spu"],
    ) -> list[int]:
        """Resolve trusted target identities without letting legacy fields choose auto scope."""
        if not request.targets:
            return []

        target_sku_ids = [
            target.sku_id for target in request.targets if target.sku_id is not None
        ]
        products_by_sku = {
            product.sku_id: product
            for product in await self._products_by_sku_ids(target_sku_ids)
        }
        resolved: list[int] = []
        for target in request.targets:
            product = products_by_sku.get(target.sku_id) if target.sku_id else None
            if (
                product is not None
                and target.spu_id is not None
                and product.spu_id != target.spu_id
            ):
                # A stale or mismatched memory pair is not a trusted identity.
                continue
            if level == "sku":
                value = product.sku_id if product is not None else None
            else:
                value = product.spu_id if product is not None else target.spu_id
            if value is not None and value not in resolved:
                resolved.append(value)
        return resolved[: request.limit]

    async def _context_target_products(
        self,
        request: CatalogSearchInput,
        level: Literal["sku", "spu"],
    ) -> list[ProductCard]:
        if level == "sku":
            sku_ids = await self._resolved_target_ids(request, "sku")
            return await self._products_by_sku_ids(sku_ids)

        spu_ids = await self._resolved_target_ids(request, "spu")
        products, _titles = await self._products_by_spu_ids(spu_ids)
        representatives: list[ProductCard] = []
        for spu_id in spu_ids:
            product = next(
                (item for item in products if item.spu_id == spu_id),
                None,
            )
            if product is not None:
                representatives.append(product)
        return representatives


    async def _products_by_sku_ids(self, sku_ids: list[int]) -> list[ProductCard]:
        if not sku_ids:
            return []
        stmt = _active_sku_rows_statement(sku_ids)
        rows = (await self.session.execute(stmt)).all()
        attributes = await CatalogRepository(self.session)._load_attributes(
            [sku.id for sku, *_ in rows]
        )
        by_id = {
            sku.id: ProductCard(
                spu_id=spu.id,
                sku_id=sku.id,
                title=sku.title,
                spu_title=spu.title,
                brand=brand.name,
                category=category.name,
                price=sku.price,
                stock=sku.stock,
                sku_sales_count=sku.sales_count,
                sales_count=spu.sales_count,
                specs={str(key): str(value) for key, value in (sku.specs_json or {}).items()}
                | attributes.get(sku.id, {}),
                image_url=sku.image_url,
            )
            for sku, spu, brand, category in rows
        }
        return [by_id[sku_id] for sku_id in sku_ids if sku_id in by_id]

    async def _products_by_spu_ids(
        self, spu_ids: list[int]
    ) -> tuple[list[ProductCard], dict[int, str]]:
        if not spu_ids:
            return [], {}
        stmt = _active_spu_rows_statement(spu_ids)
        rows = (await self.session.execute(stmt)).all()
        attributes = await CatalogRepository(self.session)._load_attributes(
            [sku.id for sku, *_ in rows]
        )
        products = []
        spu_titles: dict[int, str] = {}
        for sku, spu, brand, category in rows:
            spu_titles[spu.id] = spu.title
            products.append(
                ProductCard(
                    spu_id=spu.id,
                    sku_id=sku.id,
                    title=sku.title,
                    spu_title=spu.title,
                    brand=brand.name,
                    category=category.name,
                    price=sku.price,
                    stock=sku.stock,
                    sku_sales_count=sku.sales_count,
                    sales_count=spu.sales_count,
                    specs={
                        str(key): str(value) for key, value in (sku.specs_json or {}).items()
                    }
                    | attributes.get(sku.id, {}),
                    image_url=sku.image_url,
                )
            )
        return products, spu_titles

    async def _enrich_series_products(
        self,
        representatives: list[ProductCard],
    ) -> list[ProductCard]:
        spu_ids = list(dict.fromkeys(product.spu_id for product in representatives))
        variants, spu_titles = await self._products_by_spu_ids(spu_ids)
        series_by_id = {
            item.spu_id: item
            for item in _aggregate_product_series(variants, spu_ids, spu_titles)
        }
        enriched: list[ProductCard] = []
        for representative in representatives:
            series = series_by_id.get(representative.spu_id)
            if series is None:
                enriched.append(representative)
                continue
            enriched.append(
                representative.model_copy(
                    update={
                        "entity_scope": "spu",
                        "spu_title": series.title,
                        "series_min_price": series.min_price,
                        "series_max_price": series.max_price,
                        "series_total_stock": series.total_stock,
                        "series_sku_count": series.sku_count,
                        "series_common_specs": series.common_specs,
                        "series_option_specs": {
                            key: [value.value for value in summary.values]
                            for key, summary in series.option_specs.items()
                        },
                        "series_variants": [
                            ProductVariantCard.model_validate(
                                variant.model_dump(mode="python")
                            )
                            for variant in series.variants
                        ],
                    }
                )
            )
        return enriched

    async def _safe_plan_search(self, request: CatalogSearchInput) -> ProductQueryPlan:
        try:
            return validate_product_query_plan(await self.planner.plan_search(request))
        except (ValidationError, ValueError, TypeError) as exc:
            fallback = await RuleBasedCatalogQueryPlanner().plan_search(request)
            fallback.planner = "rule_based_fallback"
            fallback.fallback_reason = str(exc)
            try:
                return validate_product_query_plan(fallback)
            except (ValidationError, ValueError, TypeError) as fallback_exc:
                fallback.supported = False
                fallback.unsupported_reason = str(fallback_exc)
                return fallback




def _plan_dump_with_diagnostics(plan: BaseModel, diagnostics: list[ToolDiagnostic]) -> dict:
    data = plan.model_dump(mode="json")
    data["error_type"] = _diagnostic_error_type(diagnostics)
    return data


def _diagnostic_error_type(diagnostics: list[ToolDiagnostic]) -> str | None:
    for diagnostic in diagnostics:
        if diagnostic.code != "ok":
            return diagnostic.code
    return None


def _catalog_plan_diagnostics(
    plan: ProductQueryPlan,
    *,
    result_type: str,
    count: int,
) -> list[ToolDiagnostic]:
    if not plan.supported:
        if plan.usage_mapping.get("status") == "unavailable":
            return [
                ToolDiagnostic(
                    code="usage_mapping_unavailable",
                    severity="error",
                    message=plan.unsupported_reason
                    or "No deterministic specification mapping exists for this usage scenario.",
                    recommended_action="explain_limitation_and_ask_for_concrete_preferences",
                    details=plan.usage_mapping,
                )
            ]
        return [
            ToolDiagnostic(
                code="unsupported_query",
                severity="error",
                message=plan.unsupported_reason or "Catalog data does not support this query.",
                recommended_action="explain_unsupported_query",
                details={"unsupported_reason": plan.unsupported_reason},
            )
        ]
    if plan.fallback_reason:
        return [
            ToolDiagnostic(
                code="invalid_catalog_plan",
                severity="warning" if count else "error",
                message="LLM catalog planner failed validation; rule-based fallback was used.",
                recommended_action=(
                    "use_result_with_caution" if count else "ask_user_to_rephrase_or_relax_filters"
                ),
                details={"fallback_reason": plan.fallback_reason, "planner": plan.planner},
            )
        ]
    if result_type == "empty":
        return [
            ToolDiagnostic(
                code="empty_result",
                severity="info",
                message="Catalog query was valid but no products matched the filters.",
                recommended_action="relax_filters_or_ask_followup",
                details={
                    "filters": plan.filters,
                    "category": plan.category,
                    "brands": plan.brands,
                    "usage_mapping": plan.usage_mapping,
                },
            )
        ]
    if plan.normalization_debug:
        return [
            ToolDiagnostic(
                code="normalization_applied",
                severity="info",
                message="Catalog query terms were normalized before database lookup.",
                recommended_action="use_result",
                details=plan.normalization_debug,
            )
        ]
    return [
        ToolDiagnostic(
            code="ok",
            severity="info",
            message=(
                "Catalog query completed with a deterministic usage specification mapping."
                if plan.usage_mapping.get("status") in {"applied", "expanded"}
                else "Catalog query completed successfully."
            ),
            recommended_action="use_result",
            details=(plan.usage_mapping if plan.usage_mapping else {}),
        )
    ]


def _facet_plan_diagnostics(
    plan: FacetQueryPlan,
    *,
    result_type: str,
    count: int,
) -> list[ToolDiagnostic]:
    if not plan.supported:
        return [
            ToolDiagnostic(
                code="unsupported_query",
                severity="error",
                message=plan.unsupported_reason or "Catalog facet query is unsupported.",
                recommended_action="explain_unsupported_query",
                details={"unsupported_reason": plan.unsupported_reason},
            )
        ]
    if result_type == "empty":
        return [
            ToolDiagnostic(
                code="empty_result",
                severity="info",
                message="Facet query was valid but no values matched the filters.",
                recommended_action="relax_filters_or_ask_followup",
                details={"facet": plan.facet, "category": plan.category, "spec_key": plan.spec_key},
            )
        ]
    if plan.normalization_debug:
        return [
            ToolDiagnostic(
                code="normalization_applied",
                severity="info",
                message="Facet query terms were normalized before database lookup.",
                recommended_action="use_result",
                details=plan.normalization_debug,
            )
        ]
    return [
        ToolDiagnostic(
            code="ok",
            severity="info",
            message="Facet query completed successfully.",
            recommended_action="use_result",
            details={"count": count},
        )
    ]


def validate_catalog_sql(sql: str) -> None:
    """Guard for future LLM/NL2SQL planners before SQL reaches an executor."""
    normalized = re.sub(r"\s+", " ", sql.strip().lower())
    if not normalized.startswith("select "):
        raise ValueError("catalog SQL must be a SELECT statement")
    forbidden = {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "truncate",
        "create",
        "grant",
        "revoke",
    }
    if any(re.search(rf"\b{word}\b", normalized) for word in forbidden):
        raise ValueError("catalog SQL contains a forbidden operation")
    if " limit " not in f" {normalized} ":
        raise ValueError("catalog SQL must include LIMIT")

    referenced_tables = set(re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", normalized))
    unknown = referenced_tables - CATALOG_ALLOWED_TABLES
    if unknown:
        raise ValueError(f"catalog SQL references non-catalog tables: {', '.join(sorted(unknown))}")


def _active_sku_rows_statement(sku_ids: list[int]):
    return (
        select(Sku, Spu, Brand, Category)
        .join(Spu, Sku.spu_id == Spu.id)
        .join(Brand, Spu.brand_id == Brand.id)
        .join(Category, Spu.category_id == Category.id)
        .where(Sku.id.in_(sku_ids), Sku.status == 1, Spu.status == 1)
    )


def _active_spu_rows_statement(spu_ids: list[int]):
    return (
        select(Sku, Spu, Brand, Category)
        .join(Spu, Sku.spu_id == Spu.id)
        .join(Brand, Spu.brand_id == Brand.id)
        .join(Category, Spu.category_id == Category.id)
        .where(Sku.spu_id.in_(spu_ids), Sku.status == 1, Spu.status == 1)
        .order_by(Sku.spu_id, Sku.id)
    )


def validate_product_query_plan(plan: ProductQueryPlan | dict) -> ProductQueryPlan:
    if isinstance(plan, dict):
        plan = ProductQueryPlan.model_validate(plan)
    planner_supported = plan.supported
    planner_reason = plan.unsupported_reason
    # The validator, not the planner's opinion, owns the capability boundary.
    plan.supported = True
    plan.unsupported_reason = None

    normalized_category = _canonical_category(plan.category)
    if normalized_category and normalized_category.lower() not in ALLOWED_CATEGORIES:
        raise OutputValidationError(f"unsupported category: {plan.category}", path=("category",))
    plan.category = normalized_category
    if plan.ranking is not None:
        plan.sort = {
            ("price", "asc"): "price_asc",
            ("price", "desc"): "price_desc",
            ("stock", "desc"): "stock",
            ("sales", "desc"): "sales",
        }.get(
            (plan.ranking.metric, plan.ranking.direction),
            plan.sort,
        )
        plan.limit = plan.ranking.count

    if plan.usage_scenario:
        normalized_usage = _canonical_usage_scenario(plan.usage_scenario)
        if normalized_usage is None:
            plan.supported = False
            plan.unsupported_reason = plan.unsupported_reason or (
                f"unsupported usage scenario: {plan.usage_scenario}"
            )
        else:
            plan.usage_scenario = normalized_usage

    plan.filters, normalization_debug = _normalize_catalog_filters_with_debug(plan.filters)
    plan.filters, pruned_debug = _prune_redundant_filter_values(plan.filters, normalized_category)
    normalization_debug = {**normalization_debug, **pruned_debug}
    if normalization_debug:
        plan.normalization_debug = {**plan.normalization_debug, **normalization_debug}
    unknown_filters = {key for key in plan.filters if key.lower() not in ALLOWED_FILTERS}
    if unknown_filters:
        raise OutputValidationError(
            f"unsupported catalog filters: {', '.join(sorted(unknown_filters))}", path=("filters",),
        )

    normalized_category = plan.category
    if normalized_category and normalized_category in CATEGORY_FILTERS:
        disallowed_for_category = {
            key for key in plan.filters if key.lower() not in CATEGORY_FILTERS[normalized_category]
        }
        if disallowed_for_category:
            raise OutputValidationError(
                "unsupported filters for "
                f"{normalized_category}: {', '.join(sorted(disallowed_for_category))}",
                path=("filters",),
            )

    if plan.sort not in ALLOWED_SORTS:
        raise OutputValidationError(f"unsupported catalog sort: {plan.sort}", path=("sort",))

    if plan.limit < 1 or plan.limit > 20:
        raise OutputValidationError("catalog query limit must be between 1 and 20", path=("limit",))

    if plan.min_price is not None and plan.min_price < 0:
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or "min_price cannot be negative"
    if plan.max_price is not None and plan.max_price < 0:
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or "max_price cannot be negative"

    plan.requested_fields = _normalize_catalog_field_names(plan.requested_fields)
    unsupported_fields = sorted(
        set(plan.requested_fields) - CATALOG_SEARCH_FIELDS
    )
    if unsupported_fields:
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or (
            "unsupported catalog fields: " + ", ".join(unsupported_fields)
        )
    elif not plan.requested_fields and not planner_supported:
        # Compatibility safety net for an LLM response that omitted the required
        # requested_fields array. A valid populated whitelist remains authoritative.
        plan.supported = False
        plan.unsupported_reason = planner_reason or "catalog requirements are unsupported"

    if unsupported_metric := _unsupported_catalog_ranking_metric(plan.query):
        plan.supported = False
        plan.unsupported_reason = (
            f"unsupported catalog ranking metric: {unsupported_metric}"
        )

    if reason := _unsupported_reason(plan.query):
        plan.supported = False
        plan.unsupported_reason = plan.unsupported_reason or reason

    plan.filters = {key.lower(): str(value) for key, value in plan.filters.items()}
    plan.brands = _normalize_brand_list(plan.brands)
    plan.keywords = [keyword.strip() for keyword in plan.keywords if keyword.strip()]
    return plan


def _normalize_catalog_filters(filters: dict[str, str]) -> dict[str, str]:
    return _normalize_catalog_filters_with_debug(filters)[0]


def _unsupported_catalog_ranking_metric(query: str) -> str | None:
    """Reject explicit extrema whose metric is outside the retrieval ranking whitelist."""
    compact = re.sub(r"\s+", "", query.casefold())
    match = re.search(
        r"([a-z_]{2,24}|[\u4e00-\u9fff]{1,12})(?:最高|最低|最多|最少|最快|最慢)",
        compact,
    )
    if match is None:
        return None
    metric = match.group(1)
    supported_suffixes = (
        "价格",
        "价钱",
        "售价",
        "库存",
        "库存量",
        "销量",
        "price",
        "stock",
        "sales",
    )
    if any(metric.endswith(value) for value in supported_suffixes):
        return None
    return metric




def _prune_redundant_filter_values(
    filters: dict[str, str], category: str | None
) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    pruned = dict(filters)
    changes: list[dict[str, str]] = []
    if (
        category in {"headset", "headphone", "speaker"}
        and "type" in pruned
        and pruned["type"].strip().lower()
        in {"wireless", "wired", "bluetooth", "wifi", "usb", "usb-a", "usb-c"}
        and "connection_type" in pruned
    ):
        changes.append({"key": "type", "value": pruned.pop("type")})
    debug = {"pruned_filters": changes} if changes else {}
    return pruned, debug






def _canonical_usage_scenario(usage: str | None) -> str | None:
    if not usage:
        return None
    normalized = usage.strip().lower().replace("-", "_")
    if normalized in ALLOWED_USAGE_SCENARIOS:
        return normalized
    return USAGE_SCENARIO_ALIASES.get(normalized) or USAGE_SCENARIO_ALIASES.get(
        normalized.replace("_", " ")
    )


def _apply_usage_scenario_mapping(plan: ProductQueryPlan) -> ProductQueryPlan:
    if not plan.supported or not plan.usage_scenario:
        return plan

    category = _canonical_category(plan.category)
    rule = USAGE_SCENARIO_RULES.get((plan.usage_scenario, category or ""))
    if rule is None:
        return _mark_usage_mapping_unavailable(plan)

    required = list(rule.get("required", ()))
    preferred = list(rule.get("preferred", ()))
    plan.usage_mapping = {
        "status": "applied",
        "source": "deterministic_spec_mapping",
        "rule_version": USAGE_MAPPING_VERSION,
        "scenario": plan.usage_scenario,
        "category": category,
        "required": [condition.model_dump(mode="json") for condition in required],
        "preferred": [condition.model_dump(mode="json") for condition in preferred],
    }
    return plan


def _mark_usage_mapping_unavailable(plan: ProductQueryPlan) -> ProductQueryPlan:
    category = _canonical_category(plan.category)
    plan.supported = False
    plan.unsupported_reason = (
        f"No deterministic {plan.usage_scenario} specification mapping is configured "
        f"for category {category or 'unspecified'}."
    )
    plan.usage_mapping = {
        "status": "unavailable",
        "rule_version": USAGE_MAPPING_VERSION,
        "scenario": plan.usage_scenario,
        "category": category,
    }
    return plan


def _plan_to_product_search(plan: ProductQueryPlan) -> ProductSearchRequest:
    has_exclusions = bool(plan.excluded_brands or plan.excluded_usage)
    if has_exclusions:
        # Category, price and specs are already structured filters. In exclusion
        # queries, repeating localized category/usage words as title keywords can
        # eliminate every alternative before the post-retrieval exclusion pass.
        query_parts = [*plan.brands]
    elif plan.planner.startswith("rule_based") and _has_structured_catalog_constraints(plan):
        query_parts = [
            *plan.brands,
            *_product_keywords(plan),
            *_safe_query_prefilter_keywords(plan),
        ]
    elif plan.planner.startswith("rule_based"):
        query_parts = [plan.query, *plan.keywords]
    else:
        # Natural language is already represented by structured fields. Using model
        # keywords such as 144Hz, 2K or red switches as SQL title prefilters can
        # eliminate valid products before post-filtering specs.
        query_parts = [*plan.brands, *_safe_query_prefilter_keywords(plan)]
    usage_mapping_applied = plan.usage_mapping.get("status") == "applied"
    return ProductSearchRequest(
        query=" ".join(part for part in query_parts if part),
        category=plan.category,
        brands=plan.brands,
        usage_scenario=None if usage_mapping_applied else plan.usage_scenario,
        usage_required_conditions=[
            ProductSpecCondition.model_validate(item)
            for item in plan.usage_mapping.get("required", [])
        ],
        usage_preferred_conditions=[
            ProductSpecCondition.model_validate(item)
            for item in plan.usage_mapping.get("preferred", [])
        ],
        min_price=plan.min_price,
        max_price=plan.max_price,
        filters=plan.filters,
        excluded_brands=plan.excluded_brands,
        excluded_usage=plan.excluded_usage,
        sort=plan.sort,
        limit=min(20, max(12, plan.limit * 4)) if has_exclusions else plan.limit,
    )


def _has_structured_catalog_constraints(plan: ProductQueryPlan) -> bool:
    return bool(
        plan.category
        or plan.usage_scenario
        or plan.brands
        or plan.filters
        or plan.min_price is not None
        or plan.max_price is not None
    )


def _catalog_query_requests_near_match(query: str) -> bool:
    lowered = query.casefold()
    return any(
        marker in lowered
        for marker in (
            "接近",
            "最接近",
            "近似",
            "相近",
            "差不多",
            "放宽",
            "closest",
            "close to",
            "near match",
            "approximate",
        )
    )


def _catalog_filter_differences(
    product: ProductCard,
    requested_filters: dict[str, str],
) -> dict[str, dict[str, str | None]]:
    return {
        key: {
            "requested": expected,
            "actual": _catalog_product_spec_value(product, key),
        }
        for key, expected in requested_filters.items()
        if not _matches_single_filter(product.specs, key, expected)
    }


def _catalog_product_spec_value(product: ProductCard, key: str) -> str | None:
    lowered_key = key.casefold()
    return next(
        (
            str(value)
            for spec_key, value in product.specs.items()
            if spec_key.casefold() == lowered_key
        ),
        None,
    )


def _select_balanced_near_matches(
    candidates: list[ProductCard],
    differences: dict[int, dict[str, dict[str, str | None]]],
    limit: int,
) -> list[ProductCard]:
    """Prefer sales within each relaxed dimension while preserving dimension diversity."""
    buckets: dict[str, list[ProductCard]] = {}
    for product in candidates:
        relaxed_keys = tuple(differences.get(product.sku_id, {}))
        if len(relaxed_keys) != 1:
            continue
        buckets.setdefault(relaxed_keys[0], []).append(product)

    for products in buckets.values():
        products.sort(
            key=lambda product: (
                -product.sku_sales_count,
                0 if product.stock > 0 else 1,
                product.price,
                product.title,
            )
        )
    ordered_buckets = sorted(
        buckets.values(),
        key=lambda products: (
            -products[0].sku_sales_count,
            0 if products[0].stock > 0 else 1,
            products[0].price,
            products[0].title,
        ),
    )
    selected: list[ProductCard] = []
    offset = 0
    while len(selected) < limit:
        added = False
        for products in ordered_buckets:
            if offset >= len(products):
                continue
            selected.append(products[offset])
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
        offset += 1
    return selected


def _safe_query_prefilter_keywords(plan: ProductQueryPlan) -> list[str]:
    ignored = {
        "recommend",
        "recommendation",
        "find",
        "show",
        "with",
        "from",
        "under",
        "below",
        "within",
        "less",
        "than",
        "no",
        "more",
        "budget",
        "wireless",
        "wired",
        "bluetooth",
        "wifi",
        "usb",
        "usb-a",
        "usb-c",
        "switch",
        "switches",
        "red",
        "blue",
        "brown",
        "magnetic",
        "microphone",
        "mic",
        "backlit",
        "rgb",
        "2k",
        "4k",
        "1080p",
        "1440p",
        "fps",
        "gaming",
        "office",
        "video_meeting",
        "live_streaming",
        "meeting",
        "conference",
        "conferencing",
        "video",
        "zoom",
        "teams",
        "live",
        "stream",
        "streaming",
        "livestream",
        "mouse",
        "keyboard",
        "headset",
        "headphone",
        "monitor",
        "speaker",
        "webcam",
    }
    ignored.update(brand.lower() for brand in plan.brands)
    keywords: list[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9+.-]*", plan.query.lower()):
        if token in ignored or _is_spec_like_prefilter_token(token):
            continue
        if _canonical_category(token) in CATEGORY_FILTERS:
            continue
        if any(token in str(value).lower() for value in plan.filters.values()):
            continue
        keywords.append(token)
    return _dedupe_keep_order(keywords)


def _is_spec_like_prefilter_token(token: str) -> bool:
    if token.isdigit():
        return True
    return bool(re.fullmatch(r"\d+(?:\.\d+)?(?:hz|fps|w|k|p|ms|g|dpi|inch|in)", token))


def _filter_brands(products: list[ProductCard], brands: list[str]) -> list[ProductCard]:
    if not brands:
        return products
    lowered = [brand.lower() for brand in brands]
    return [
        product for product in products if any(brand in product.brand.lower() for brand in lowered)
    ]


def _merge_catalog_product_sets(
    first: list[ProductCard],
    second: list[ProductCard],
    *,
    scope: Literal["sku", "spu"],
) -> list[ProductCard]:
    merged: list[ProductCard] = []
    seen: set[int] = set()
    for product in [*first, *second]:
        identity = product.sku_id if scope == "sku" else product.spu_id
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(product)
    return merged


def _round_robin_category_products(
    category_results: list[tuple[str, ProductQueryPlan, list[ProductCard], int]],
    *,
    limit: int,
) -> list[ProductCard]:
    selected: list[ProductCard] = []
    seen_sku_ids: set[int] = set()
    max_bucket_size = max(
        (len(products) for _, _, products, _ in category_results),
        default=0,
    )
    for product_index in range(max_bucket_size):
        for _category, _plan, products, _total_count in category_results:
            if product_index >= len(products):
                continue
            product = products[product_index]
            if product.sku_id in seen_sku_ids:
                continue
            selected.append(product)
            seen_sku_ids.add(product.sku_id)
            if len(selected) >= limit:
                return selected
    return selected


def _filters_from_query(query: str) -> dict[str, str]:
    lowered = query.lower()
    filters: dict[str, str] = {}
    if any(term in lowered for term in {"usb-c", "usb c", "type-c", "type c"}):
        filters["connection_type"] = "USB-C"
    elif any(term in lowered for term in {"usb-a", "usb a", "type-a", "type a"}):
        filters["connection_type"] = "USB-A"
    elif any(term in lowered for term in {"bluetooth", "蓝牙"}):
        filters["connection_type"] = "蓝牙"
    elif any(term in lowered for term in {"2.4g", "2.4ghz"}):
        filters["connection_type"] = "2.4G 无线"
    elif any(term in lowered for term in {"三模", "tri-mode", "tri mode"}):
        filters["connection_type"] = "三模"
    elif any(
        term in lowered
        for term in {"wireless", "wifi", "无线"}
    ):
        filters["connection_type"] = "Wireless"
    elif re.search(r"(?<![a-z0-9])usb(?![a-z0-9-])", lowered):
        filters["connection_type"] = "USB"
    elif any(term in lowered for term in {"wired", "cable", "有线"}):
        filters["connection_type"] = "Wired"

    if color := _color_from_query(lowered):
        filters["color"] = color
    if match := re.search(r"(\d{2,3})\s*(?:hz|赫兹)", lowered):
        filters["refresh_rate"] = f"{match.group(1)}Hz"
    if match := re.search(r"(\d{2,3})\s*fps", lowered):
        filters["frame_rate"] = f"{match.group(1)}fps"
    if match := re.search(r"(\d{1,4})\s*w\b", lowered):
        filters["power_w"] = f"{match.group(1)}W"
    if any(term in lowered for term in {"2k", "1440p"}):
        filters["resolution"] = "2560x1440"
    elif "4k" in lowered:
        filters["resolution"] = "4K"
    elif "1080p" in lowered:
        filters["resolution"] = "1080p"

    if any(term in lowered for term in {"red switch", "red switches", "红轴"}):
        filters["switches"] = "Red"
    elif any(term in lowered for term in {"blue switch", "blue switches", "青轴"}):
        filters["switches"] = "Blue"
    elif any(term in lowered for term in {"brown switch", "brown switches", "茶轴"}):
        filters["switches"] = "Brown"
    elif any(term in lowered for term in {"magnetic switch", "magnetic switches", "磁轴"}):
        filters["switches"] = "Magnetic"

    if any(term in lowered for term in {"microphone", "mic", "麦克风", "带麦"}):
        filters["microphone"] = "Yes"
    if any(term in lowered for term in {"backlit", "rgb", "背光", "灯光"}):
        filters["backlit"] = "Yes"

    return filters


def _color_from_query(lowered_query: str) -> str | None:
    color_terms = (
        ("black", "Black"),
        ("white", "White"),
        ("silver", "Silver"),
        ("gray", "Gray"),
        ("grey", "Gray"),
        ("pink", "Pink"),
        ("黑色", "黑色"),
        ("白色", "白色"),
        ("银色", "银色"),
        ("灰色", "灰色"),
        ("粉色", "粉色"),
    )
    for term, color in color_terms:
        if term in lowered_query:
            return color
    return None


def _max_price_from_query(query: str) -> Decimal | None:
    lowered = query.lower().replace(",", "")
    patterns = (
        r"(?:under|below|within|less than|<=|no more than)\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:元|rmb|cny|usd|dollars?)?\s*(?:以内|以下|以下的|预算内)",
    )
    for pattern in patterns:
        if match := re.search(pattern, lowered):
            return Decimal(match.group(1))
    return None


def _infer_brands_from_text(query: str) -> list[str]:
    lowered = query.lower()
    brands = [brand for brand in KNOWN_BRANDS if brand.lower() in lowered]
    for alias, brand in BRAND_ALIASES.items():
        if alias.lower() in lowered:
            brands.append(brand)
    return _dedupe_keep_order(brands)


def _normalize_brand_list(brands: list[str]) -> list[str]:
    normalized = []
    for brand in brands:
        canonical = _canonical_brand(brand)
        if canonical:
            normalized.append(canonical)
    return _dedupe_keep_order(normalized)


def _canonical_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    stripped = brand.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    for known_brand in KNOWN_BRANDS:
        if lowered == known_brand.lower():
            return known_brand
    return BRAND_ALIASES.get(stripped) or BRAND_ALIASES.get(lowered) or stripped


def _unsupported_reason(query: str) -> str | None:
    lowered = query.lower()
    for pattern, reason in UNSUPPORTED_QUERY_PATTERNS.items():
        if pattern in lowered:
            return reason
    return None




def _product_keywords(plan: ProductQueryPlan) -> list[str]:
    ignored = {
        "recommend",
        "recommendation",
        "wireless",
        "wired",
        "fps",
        "gaming",
        "office",
        "video_meeting",
        "live_streaming",
        "meeting",
        "conference",
        "conferencing",
        "video",
        "zoom",
        "teams",
        "live",
        "stream",
        "streaming",
        "livestream",
        "mouse",
        "keyboard",
        "headset",
        "headphone",
        "monitor",
        "speaker",
        "webcam",
    }
    return [
        keyword
        for keyword in plan.keywords
        if keyword.lower() not in ignored and _canonical_category(keyword) not in CATEGORY_FILTERS
    ]


def _catalog_planner_system_prompt() -> str:
    return load_prompt("catalog/compatibility_plan.md")


def _retry_feedback(
    plan_data: dict | None, exc: ValidationError | OutputValidationError, raw_output: str,
) -> dict:
    parsed = plan_data if plan_data is not None else {}
    category = _canonical_category(str(parsed.get("category") or ""))
    allowed_for_category = (
        sorted(CATEGORY_FILTERS[category]) if category and category in CATEGORY_FILTERS else None
    )
    return {
        **validation_feedback(exc),
        "stage": "json_parse" if plan_data is None else "plan_validation",
        "raw_output": raw_output,
        "parsed_output": plan_data,
        "error": str(exc),
        "received_category": parsed.get("category"),
        "received_filters": parsed.get("filters"),
        "allowed_categories": sorted(CATEGORY_FILTERS),
        "allowed_filters": sorted(ALLOWED_FILTERS),
        "allowed_filters_for_received_category": allowed_for_category,
        "normalization_hints": _normalization_hints(),
    }


def _normalization_hints() -> dict[str, dict[str, str]]:
    return {
        "bluetooth": {"key": "connection_type", "value": "蓝牙"},
        "wifi": {"key": "connection_type", "value": "Wireless"},
        "wireless": {"key": "connection_type", "value": "Wireless"},
        "蓝牙": {"key": "connection_type", "value": "蓝牙"},
        "无线": {"key": "connection_type", "value": "Wireless"},
        "三模": {"key": "connection_type", "value": "三模"},
        "2.4G": {"key": "connection_type", "value": "2.4G 无线"},
        "wired": {"key": "connection_type", "value": "Wired"},
        "USB": {"key": "connection_type", "value": "USB"},
        "USB-C": {"key": "connection_type", "value": "USB-C"},
        "USB-A": {"key": "connection_type", "value": "USB-A"},
        "有线": {"key": "connection_type", "value": "Wired"},
        "30W": {"key": "power_w", "value": "30"},
        "20W": {"key": "power_w", "value": "20"},
        "red switch": {"key": "switches", "value": "Red"},
        "红轴": {"key": "switches", "value": "红轴"},
        "2K": {"key": "resolution", "value": "2560x1440"},
        "144Hz": {"key": "refresh_rate", "value": "144Hz"},
        "60fps": {"key": "frame_rate", "value": "60fps"},
        "with microphone": {"key": "microphone", "value": "Yes"},
    }


def _validate_product_plan_data(plan_data: dict) -> None:
    probe = dict(plan_data)
    probe.setdefault("query", "validation probe")
    probe.setdefault("limit", 3)
    _coerce_filter_values_to_strings(probe)
    validate_product_query_plan(ProductQueryPlan.model_validate(probe))


def _coerce_filter_values_to_strings(plan_data: dict) -> None:
    filters = plan_data.get("filters")
    if not isinstance(filters, dict):
        return
    plan_data["filters"] = {str(key): str(value) for key, value in filters.items()}


def _extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise OutputValidationError(
                "LLM planner did not return a JSON object",
                error_type=OutputErrorType.FORMAT_ERROR, code="invalid_json",
            ) from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise OutputValidationError(
            "LLM planner JSON must be an object", error_type=OutputErrorType.SCHEMA_ERROR,
            code="object_type",
        )
    return parsed


def _message_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _sort_from_query(
    query: str,
) -> Literal["recommend", "sales", "price_asc", "price_desc", "stock"] | None:
    lowered = query.casefold().replace(" ", "")
    if any(
        marker in lowered
        for marker in (
            "最贵",
            "价格最高",
            "价钱最高",
            "售价最高",
            "价格从高到低",
            "价格降序",
            "highestprice",
            "mostexpensive",
            "priciest",
        )
    ):
        return "price_desc"
    if any(
        marker in lowered
        for marker in (
            "最便宜",
            "价格最低",
            "价钱最低",
            "售价最低",
            "价格从低到高",
            "价格升序",
            "lowestprice",
            "cheapest",
            "leastexpensive",
        )
    ):
        return "price_asc"
    if any(
        marker in lowered
        for marker in (
            "销量最高",
            "销量最好",
            "销量排行",
            "销量排名",
            "销量第",
            "销量前",
            "按销量",
            "最畅销",
            "最热销",
            "bestselling",
            "topselling",
        )
    ):
        return "sales"
    if any(
        marker in lowered
        for marker in (
            "库存最多",
            "库存最高",
            "库存最足",
            "库存量最高",
            "按库存",
            "库存降序",
            "moststock",
            "higheststock",
        )
    ):
        return "stock"
    return None


def _ranking_plan_from_query(
    query: str,
    default_count: int,
    *,
    has_context_target: bool = False,
) -> CatalogRankingPlan | None:
    compact = re.sub(r"\s+", "", query.casefold())
    if any(marker in compact for marker in ("库存最少", "库存最低", "leaststock")):
        metric, direction = "stock", "asc"
    elif any(
        marker in compact
        for marker in ("销量最低", "销量最少", "最不畅销", "leastsales", "lowestsales")
    ):
        metric, direction = "sales", "asc"
    else:
        sort = _sort_from_query(query)
        metric_by_sort = {
            "price_asc": ("price", "asc"),
            "price_desc": ("price", "desc"),
            "stock": ("stock", "desc"),
            "sales": ("sales", "desc"),
        }
        if sort not in metric_by_sort:
            return None
        metric, direction = metric_by_sort[sort]
    rank = _rank_position_from_query(query) or 1
    explicit_count = _requested_product_count(query)
    count = explicit_count or (1 if _is_extreme_query(query) or rank > 1 else default_count)
    return CatalogRankingPlan(
        scope=_catalog_selection_scope_from_query(
            query,
            has_context_target=has_context_target,
        ),
        metric=metric,
        direction=direction,
        rank=rank,
        count=count,
    )


def _catalog_selection_scope_from_query(
    query: str,
    *,
    has_context_target: bool = False,
) -> Literal["sku", "spu"]:
    compact = re.sub(r"\s+", "", query.casefold())
    if "sku" in compact:
        return "sku"
    if "spu" in compact:
        return "spu"
    if _catalog_query_explicitly_requests_spu(query):
        return "spu"

    explicit_variant_metric = (
        r"(?:版本|变体).*(?:销量|库存|价格|售价|最便宜|最贵|排行|排名)",
        r"(?:销量|库存|价格|售价).*(?:版本|变体)",
        r"(?:单版本|单个版本|具体版本)",
    )
    if any(re.search(pattern, compact) for pattern in explicit_variant_metric):
        return "sku"
    if compact.count("版本") >= 2 or compact.count("sku") >= 2:
        return "sku"

    variant_selection_markers = (
        "哪个版本",
        "哪一个版本",
        "哪款版本",
        "各版本",
        "各个版本",
        "全部版本",
        "所有版本",
        "其他版本",
        "其它版本",
        "不同版本",
        "版本之间",
        "版本中",
        "版本里",
    )
    if any(marker in compact for marker in variant_selection_markers):
        return "sku"

    parent_reference_markers = (
        "这个",
        "这款",
        "该款",
        "当前",
        "上述",
        "上面",
        "刚才",
        "它的",
    )
    has_parent_reference = has_context_target or any(
        marker in compact for marker in parent_reference_markers
    )
    has_model_identifier = bool(
        re.search(
            r"(?<![a-z0-9])(?:[a-z]{1,12}[-_]?\d{1,6}|\d{1,6}[a-z]{1,8})(?![a-z0-9])",
            query.casefold(),
        )
    )
    concrete_variant_markers = (
        "版本",
        "版",
        "颜色",
        "配色",
        "轴体",
        "红轴",
        "青轴",
        "茶轴",
        "磁轴",
        "连接版本",
        "有线",
        "无线",
        "蓝牙",
        "2.4g",
        "标准版",
        "增强版",
    )
    has_concrete_variant = any(
        marker in compact for marker in concrete_variant_markers
    )
    if has_concrete_variant and (has_parent_reference or has_model_identifier):
        return "sku"
    return "spu"


def _resolved_catalog_selection_scope(
    query: str,
    *,
    has_context_target: bool,
    planned_scope: Literal["sku", "spu"] | None,
    result_purpose: Literal["recommendation", "search", "lookup", "ranking"],
    filters: dict[str, str],
    has_price_constraint: bool,
) -> Literal["sku", "spu"]:
    """Resolve the final display entity from validated query semantics.

    The planner extracts structured filters and purpose, while deterministic rules keep
    explicit series/SKU wording authoritative. Purchase-ready recommendations use concrete
    SKUs whenever the answer depends on variant-level eligibility or offer facts.
    """
    if _catalog_query_explicitly_requests_spu(query):
        return "spu"
    rule_scope = _catalog_selection_scope_from_query(
        query,
        has_context_target=has_context_target,
    )
    if rule_scope == "sku":
        return "sku"
    recommendation = (
        result_purpose == "recommendation"
        or _catalog_query_requests_recommendation(query)
    )
    if recommendation and (
        filters
        or has_price_constraint
        or _catalog_query_requests_concrete_offer_facts(query)
        or planned_scope == "sku"
    ):
        return "sku"
    return "spu"


def _catalog_query_explicitly_requests_spu(query: str) -> bool:
    compact = re.sub(r"\s+", "", query.casefold())
    return any(
        marker in compact
        for marker in (
            "系列的全部版本",
            "系列的所有版本",
            "系列全部版本",
            "系列所有版本",
            "整个系列",
            "完整系列",
        )
    ) or bool(re.search(r"(?:两个|两款|多个).{0,12}系列", compact))


def _catalog_query_requests_recommendation(query: str) -> bool:
    compact = re.sub(r"\s+", "", query.casefold())
    return any(
        marker in compact
        for marker in (
            "推荐",
            "首选",
            "值得买",
            "值得推荐",
            "怎么选",
            "帮我选",
            "适合我",
            "替代款",
            "recommend",
            "suggest",
            "alternative",
        )
    )


def _catalog_query_requests_concrete_offer_facts(query: str) -> bool:
    compact = re.sub(r"\s+", "", query.casefold())
    return any(
        marker in compact
        for marker in (
            "价格",
            "售价",
            "多少钱",
            "库存",
            "有货",
            "现货",
            "规格",
            "参数",
            "配置",
            "price",
            "stock",
            "spec",
            "configuration",
        )
    )


def _catalog_result_purpose_from_query(
    query: str,
    *,
    has_context_target: bool = False,
    has_ranking: bool = False,
    planned_purpose: Literal["recommendation", "search", "lookup", "ranking"] | None = None,
) -> Literal["recommendation", "search", "lookup", "ranking"]:
    if has_ranking:
        return "ranking"

    compact = re.sub(r"\s+", "", query.casefold())
    if _catalog_query_requests_recommendation(query):
        return "recommendation"

    lookup_markers = (
        "多少钱",
        "价格多少",
        "售价多少",
        "库存多少",
        "有货吗",
        "有现货吗",
        "什么规格",
        "规格是什么",
        "参数是什么",
        "详细信息",
        "有哪些版本",
        "所有版本",
        "全部版本",
        "各版本",
    )
    has_model_identifier = bool(
        re.search(
            r"(?<![a-z0-9])(?:[a-z]{1,12}[-_]?\d{1,6}|\d{1,6}[a-z]{1,8})(?![a-z0-9])",
            query.casefold(),
        )
    )
    has_parent_reference = any(
        marker in compact
        for marker in ("这个", "这款", "该款", "当前", "上述", "上面", "刚才", "它的")
    )
    if any(marker in compact for marker in lookup_markers) and (
        has_context_target or has_parent_reference or has_model_identifier or "版本" in compact
    ):
        return "lookup"
    if has_context_target and any(
        marker in compact
        for marker in ("这个", "这款", "该款", "当前", "它的", "看看", "查看")
    ):
        return "lookup"

    search_markers = (
        "有哪些",
        "找一下",
        "查一下",
        "搜索",
        "看看",
        "筛选",
        "列出",
        "show",
        "find",
        "search",
        "list",
    )
    if any(marker in compact for marker in search_markers):
        return "search"
    return planned_purpose or "search"


def _rank_position_from_query(query: str) -> int | None:
    compact = re.sub(r"\s+", "", query.casefold())
    if match := re.search(r"(?:第|rank)(\d{1,2})", compact):
        rank = int(match.group(1))
        return rank if 1 <= rank <= 20 else None
    if match := re.search(r"第([一二两三四五六七八九十]+)", compact):
        rank = _parse_small_chinese_number(match.group(1))
        return rank if rank is not None and 1 <= rank <= 20 else None
    return None


def _is_extreme_query(query: str) -> bool:
    compact = re.sub(r"\s+", "", query.casefold())
    return any(
        marker in compact
        for marker in (
            "最便宜",
            "最贵",
            "价格最低",
            "价格最高",
            "库存最多",
            "库存最高",
            "库存最足",
            "库存最少",
            "库存最低",
            "销量最高",
            "销量最好",
            "销量最低",
            "销量最少",
            "最不畅销",
            "最畅销",
            "最热销",
            "cheapest",
            "mostexpensive",
            "highestprice",
            "moststock",
            "higheststock",
            "bestselling",
            "topselling",
        )
    )


def _requested_product_count(query: str) -> int | None:
    normalized = query.casefold().replace("，", " ").replace(",", " ")
    if top_match := re.search(r"(?:前|top)\s*(\d{1,2})(?!\s*(?:名次|rank))", normalized):
        value = int(top_match.group(1))
        return value if 1 <= value <= 20 else None
    digit_match = re.search(
        r"(?:前\s*|top\s*)?(\d{1,2})\s*(?:款|个|台|种|件|名|products?|items?|models?)",
        normalized,
    )
    if digit_match:
        value = int(digit_match.group(1))
        return value if 1 <= value <= 20 else None

    chinese_match = re.search(
        r"(?:前\s*)?([一二两三四五六七八九十]+)\s*(?:款|个|台|种|件|名)",
        normalized,
    )
    if not chinese_match:
        return None
    value = _parse_small_chinese_number(chinese_match.group(1))
    return value if value is not None and 1 <= value <= 20 else None


def _parse_small_chinese_number(value: str) -> int | None:
    digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value in digits:
        return digits[value]
    if value == "十":
        return 10
    if "十" not in value or value.count("十") != 1:
        return None
    tens, ones = value.split("十", maxsplit=1)
    tens_value = digits.get(tens, 1) if tens else 1
    ones_value = digits.get(ones, 0) if ones else 0
    return tens_value * 10 + ones_value


def _apply_high_confidence_search_overrides(
    plan: ProductQueryPlan,
    query: str,
) -> None:
    """Apply only unambiguous lexical constraints after the LLM has classified the task."""
    inferred_category = _infer_category_from_text(query.lower())
    if not plan.category and inferred_category:
        plan.category = inferred_category
    if not plan.brands:
        plan.brands = _infer_brands_from_text(query)
    inferred_max_price = _max_price_from_query(query)
    if plan.max_price is None and inferred_max_price is not None:
        plan.max_price = inferred_max_price
    inferred_filters = _filters_from_query(query)
    for key, value in inferred_filters.items():
        if key == "connection_type" and value in {
            "USB",
            "USB-A",
            "USB-C",
            "蓝牙",
            "2.4G 无线",
            "三模",
        }:
            # Explicit connection details are a safe lexical override. Do not let
            # a broader LLM-authored Wired/Wireless class erase USB-C, Bluetooth,
            # 2.4G, or tri-mode intent.
            plan.filters[key] = value
        else:
            plan.filters.setdefault(key, value)
    if plan.usage_scenario is None:
        plan.usage_scenario = _usage_from_query(query)
    inferred_sort = _sort_from_query(query)
    if inferred_sort is not None:
        plan.sort = inferred_sort
    inferred_ranking = _ranking_plan_from_query(query, plan.limit)
    if inferred_ranking is not None:
        plan.ranking = inferred_ranking
        plan.limit = inferred_ranking.count
        plan.result_purpose = "ranking"
        plan.selection_scope = inferred_ranking.scope
        plan.ranking = plan.ranking.model_copy(
            update={"scope": plan.selection_scope}
        )
    compact = re.sub(r"\s+", "", query.casefold())
    if _catalog_query_requests_recommendation(query):
        plan.result_purpose = "recommendation"
    elif re.search(
        r"(?:一共|共有|总共|总计|多少)(?:有)?(?:个|款|种)?(?:商品|产品|键盘|鼠标|耳机|"
        r"显示器|音箱|摄像头|系列|sku|spu)",
        compact,
    ):
        plan.result_purpose = "count"

    if re.search(r"(?<![a-z0-9])sku(?![a-z0-9])", query.casefold()):
        plan.selection_scope = "sku"
    elif any(
        marker in compact
        for marker in (
            "有哪些版本",
            "所有版本",
            "全部版本",
            "各版本",
            "可售版本",
            "variantoptions",
        )
    ):
        plan.selection_scope = "sku"
    elif re.search(r"(?<![a-z0-9])spu(?![a-z0-9])", query.casefold()) or (
        _catalog_query_explicitly_requests_spu(query)
    ):
        plan.selection_scope = "spu"
    if plan.ranking is not None:
        plan.ranking = plan.ranking.model_copy(update={"scope": plan.selection_scope})
    if plan.result_purpose == "recommendation" and plan.sort == "recommend":
        plan.sort = "sales"
    if _is_supported_sales_rank_query(query):
        plan.supported = True
        plan.unsupported_reason = None


def _normalize_catalog_field_names(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        field_name = re.sub(
            r"[^a-z0-9_]+",
            "_",
            str(value).strip().casefold(),
        ).strip("_")
        if field_name and field_name not in normalized:
            normalized.append(field_name)
    return normalized


def _deterministic_catalog_search_fields(query: str) -> list[str]:
    """Minimal offline fallback; the production LLM preserves less obvious requirements."""
    compact = re.sub(r"\s+", "", query.casefold())
    fields = ["product_identity"]
    markers = {
        "price": ("价格", "售价", "多少钱", "便宜", "最贵", "price"),
        "stock": ("库存", "有货", "现货", "stock"),
        "sales_count": ("销量", "卖得", "sales"),
        "variants": ("所有版本", "全部版本", "各版本", "variant"),
        "specifications": ("规格", "参数", "配置", "spec"),
    }
    for field_name, terms in markers.items():
        if any(term in compact for term in terms):
            fields.append(field_name)
    return fields


def _is_supported_sales_rank_query(query: str) -> bool:
    compact = re.sub(r"\s+", "", query.casefold())
    rank_markers = (
        "销量第",
        "销量排名",
        "销量排行",
        "salesrank",
        "salesranking",
    )
    return _unsupported_reason(query) is None and any(marker in compact for marker in rank_markers)


def _is_spu_sales_rank_query(query: str) -> bool:
    if not _is_supported_sales_rank_query(query):
        return False
    compact = re.sub(r"\s+", "", query.casefold())
    explicit_sku_scope = ("sku", "版本", "颜色")
    return not any(marker in compact for marker in explicit_sku_scope)


def _apply_explicit_overrides(plan: ProductQueryPlan, overrides: dict) -> None:
    if overrides.get("category"):
        plan.category = overrides["category"]
    if overrides.get("brands"):
        plan.brands = overrides["brands"]
    if overrides.get("min_price") is not None:
        plan.min_price = overrides["min_price"]
    if overrides.get("max_price") is not None:
        plan.max_price = overrides["max_price"]
    if overrides.get("filters"):
        plan.filters = {**plan.filters, **overrides["filters"]}
    if overrides.get("keywords"):
        plan.keywords = list(overrides["keywords"])
    if overrides.get("sort"):
        plan.sort = overrides["sort"]
    if overrides.get("excluded_brands"):
        plan.excluded_brands = _dedupe_keep_order(
            [*plan.excluded_brands, *overrides["excluded_brands"]]
        )
        excluded = {item.lower() for item in overrides["excluded_brands"]}
        plan.brands = [item for item in plan.brands if item.lower() not in excluded]
    if overrides.get("excluded_usage"):
        plan.excluded_usage = _dedupe_keep_order(
            [*plan.excluded_usage, *overrides["excluded_usage"]]
        )
        if plan.usage_scenario in overrides["excluded_usage"]:
            plan.usage_scenario = None
            plan.keywords = [
                item for item in plan.keywords if item not in overrides["excluded_usage"]
            ]
    if overrides.get("brands") and not overrides.get("excluded_brands"):
        included = {item.lower() for item in plan.brands}
        plan.excluded_brands = [
            item for item in plan.excluded_brands if item.lower() not in included
        ]
    if overrides.get("usage_scenario"):
        plan.usage_scenario = overrides["usage_scenario"]
        plan.keywords = _dedupe_keep_order([*plan.keywords, str(overrides["usage_scenario"])])


def _apply_preference_defaults(plan: ProductQueryPlan, defaults: dict) -> None:
    if not plan.brands and defaults.get("brands"):
        plan.brands = list(defaults["brands"])
    plan.excluded_brands = _dedupe_keep_order(
        [*plan.excluded_brands, *defaults.get("excluded_brands", [])]
    )
    plan.excluded_usage = _dedupe_keep_order(
        [*plan.excluded_usage, *defaults.get("excluded_usage", [])]
    )
    excluded_brands = {item.lower() for item in plan.excluded_brands}
    plan.brands = [item for item in plan.brands if item.lower() not in excluded_brands]
    if plan.usage_scenario in plan.excluded_usage:
        plan.usage_scenario = None
        plan.keywords = [item for item in plan.keywords if item not in plan.excluded_usage]
    if plan.max_price is None and defaults.get("max_price") is not None:
        plan.max_price = defaults["max_price"]
    connection_type = defaults.get("connection_type")
    if connection_type and "connection_type" not in plan.filters:
        plan.filters["connection_type"] = connection_type
    usage = defaults.get("usage")
    if plan.usage_scenario is None and usage:
        plan.usage_scenario = str(usage)
        plan.keywords = _dedupe_keep_order([*plan.keywords, str(usage)])


def _json_safe(data: dict) -> dict:
    return json.loads(json.dumps(data, default=str))


def _usage_from_query(query: str) -> str | None:
    lowered = query.lower()
    if _contains_any_usage_term(
        lowered,
        {"live streaming", "livestream", "直播", "主播", "开播"},
    ):
        return "live_streaming"
    if _contains_any_usage_term(
        lowered,
        {
            "video meeting",
            "video conference",
            "video conferencing",
            "zoom",
            "teams",
            "开会",
            "会议",
            "网课",
            "视频通话",
        },
    ):
        return "video_meeting"
    if _contains_any_usage_term(
        lowered,
        {"fps", "game", "gaming", "esports", "游戏", "电竞"},
    ):
        return "gaming"
    if _contains_any_usage_term(
        lowered,
        {"office", "办公", "码字", "写代码", "生产力", "学习"},
    ):
        return "office"
    return None


def _contains_any_usage_term(query: str, terms: set[str]) -> bool:
    for term in terms:
        if term.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", query):
                return True
        elif term in query:
            return True
    return False


def _filter_excluded_preferences(
    products: list[ProductCard], excluded_brands: list[str], excluded_usage: list[str]
) -> list[ProductCard]:
    brand_terms = {item.lower() for item in excluded_brands}
    usage_terms = {
        term
        for usage in excluded_usage
        for term in {
            usage.lower(),
            "游戏" if usage.lower() == "gaming" else "办公" if usage.lower() == "office" else "",
        }
        if term
    }

    def allowed(product: ProductCard) -> bool:
        if product.brand.lower() in brand_terms:
            return False
        haystack = " ".join(
            [product.title, product.category, *[str(value) for value in product.specs.values()]]
        ).lower()
        return not any(term in haystack for term in usage_terms)

    return [product for product in products if allowed(product)]


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    deduped: list[str] = []
    for item in items:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _with_sku_ranking(
    product: ProductCard,
    ranking: CatalogRankingPlan,
) -> ProductCard:
    values = {
        "price": product.price,
        "stock": Decimal(product.stock),
        "sales": Decimal(product.sku_sales_count),
    }
    return product.model_copy(
        update={
            "ranking_scope": "sku",
            "ranking_metric": ranking.metric,
            "ranking_value": values[ranking.metric],
        }
    )


def _aggregate_product_series(
    products: list[ProductCard],
    spu_ids: list[int],
    spu_titles: dict[int, str],
) -> list[CatalogSeriesSummary]:
    grouped: dict[int, list[ProductCard]] = {}
    for product in products:
        grouped.setdefault(product.spu_id, []).append(product)

    series: list[CatalogSeriesSummary] = []
    for spu_id in spu_ids:
        variants = grouped.get(spu_id, [])
        if not variants:
            continue
        normalized_specs = [_normalized_series_specs(product.specs) for product in variants]
        all_keys = sorted({key for specs in normalized_specs for key in specs})
        common_specs: dict[str, str] = {}
        option_specs: dict[str, CatalogSeriesSpecSummary] = {}
        for key in all_keys:
            present = [
                (specs[key], variants[index].stock > 0)
                for index, specs in enumerate(normalized_specs)
                if key in specs
            ]
            values = _series_value_summaries(present)
            missing_count = len(variants) - len(present)
            if missing_count == 0 and len(values) == 1:
                common_specs[key] = values[0].value
            else:
                option_specs[key] = CatalogSeriesSpecSummary(
                    present_sku_count=len(present),
                    missing_sku_count=missing_count,
                    values=values,
                )

        first = variants[0]
        prices = [product.price for product in variants]
        series.append(
            CatalogSeriesSummary(
                spu_id=spu_id,
                title=spu_titles.get(spu_id, first.title),
                brand=first.brand,
                category=first.category,
                sales_count=first.sales_count,
                sku_count=len(variants),
                in_stock_sku_count=sum(product.stock > 0 for product in variants),
                total_stock=sum(max(0, product.stock) for product in variants),
                min_price=min(prices),
                max_price=max(prices),
                common_specs=common_specs,
                option_specs=option_specs,
                variants=[
                    CatalogSeriesVariant(
                        sku_id=product.sku_id,
                        title=product.title,
                        price=product.price,
                        stock=product.stock,
                        sku_sales_count=product.sku_sales_count,
                        specs=normalized_specs[index],
                        image_url=product.image_url,
                    )
                    for index, product in enumerate(variants)
                ],
            )
        )
    return series


def _normalized_series_specs(specs: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in specs.items():
        key = _normalize_filter_key(str(raw_key))
        value = str(raw_value).strip()
        if key and value:
            normalized[key] = value
    return normalized


def _series_value_summaries(
    values: list[tuple[str, bool]],
) -> list[CatalogSeriesSpecValue]:
    counts: dict[str, tuple[str, int, int]] = {}
    for value, in_stock in values:
        identity = value.casefold()
        display, sku_count, stock_count = counts.get(identity, (value, 0, 0))
        counts[identity] = (display, sku_count + 1, stock_count + int(in_stock))
    return [
        CatalogSeriesSpecValue(
            value=display,
            sku_count=sku_count,
            in_stock_sku_count=stock_count,
        )
        for display, sku_count, stock_count in sorted(
            counts.values(),
            key=lambda item: item[0].casefold(),
        )
    ]


def _facet_count_scope_from_query(query: str) -> Literal["sku", "spu"]:
    compact = re.sub(r"\s+", "", query.casefold())
    if any(
        marker in compact
        for marker in (
            "sku",
            "多少个版本",
            "几个版本",
            "版本数",
            "变体数",
        )
    ):
        return "sku"
    return "spu"




def _facet_query_plan(request: CatalogFacetInput) -> FacetQueryPlan:
    normalized_request = _normalize_facet_request(request)
    filters, normalization_debug = _normalize_catalog_filters_with_debug(normalized_request.filters)
    spec_key = (
        _normalize_filter_key(normalized_request.spec_key) if normalized_request.spec_key else None
    )
    category = _canonical_category(normalized_request.category)
    plan = FacetQueryPlan(
        query=normalized_request.query,
        facet=normalized_request.facet,
        category=category,
        brand=normalized_request.brand,
        spec_key=spec_key,
        min_price=normalized_request.min_price,
        max_price=normalized_request.max_price,
        filters={key.lower(): str(value) for key, value in filters.items()},
        count_scope=_facet_count_scope_from_query(normalized_request.query),
        limit=normalized_request.limit,
        normalization_debug=normalization_debug,
    )

    if category and category not in ALLOWED_CATEGORIES:
        plan.supported = False
        plan.unsupported_reason = f"unsupported category: {category}"
        return plan

    unknown_filters = {key for key in plan.filters if key.lower() not in ALLOWED_FILTERS}
    if unknown_filters:
        plan.supported = False
        plan.unsupported_reason = "unsupported catalog filters: " + ", ".join(
            sorted(unknown_filters)
        )
        return plan

    if spec_key and spec_key.lower() not in ALLOWED_FILTERS:
        plan.supported = False
        plan.unsupported_reason = f"unsupported spec_key: {spec_key}"
        return plan

    if plan.facet == "spec_value" and not spec_key:
        inferred_spec_key = _infer_spec_key_from_text(plan.query)
        if inferred_spec_key:
            spec_key = inferred_spec_key
            plan.spec_key = inferred_spec_key
        else:
            plan.supported = False
            plan.unsupported_reason = "spec_value facet requires spec_key"
            return plan

    if category and category in CATEGORY_FILTERS:
        allowed = CATEGORY_FILTERS[category]
        disallowed_filters = {key for key in plan.filters if key.lower() not in allowed}
        if disallowed_filters:
            plan.supported = False
            plan.unsupported_reason = f"unsupported filters for {category}: " + ", ".join(
                sorted(disallowed_filters)
            )
            return plan
        if spec_key and spec_key.lower() not in allowed:
            plan.supported = False
            plan.unsupported_reason = f"unsupported spec_key for {category}: {spec_key}"
            return plan

    if plan.min_price is not None and plan.min_price < 0:
        plan.supported = False
        plan.unsupported_reason = "min_price cannot be negative"
        return plan
    if plan.max_price is not None and plan.max_price < 0:
        plan.supported = False
        plan.unsupported_reason = "max_price cannot be negative"
        return plan
    if (
        plan.min_price is not None
        and plan.max_price is not None
        and plan.min_price > plan.max_price
    ):
        plan.supported = False
        plan.unsupported_reason = "min_price cannot be greater than max_price"
        return plan

    if reason := _unsupported_reason(plan.query):
        plan.supported = False
        plan.unsupported_reason = reason

    return _validate_catalog_facet_plan(plan)


def _normalize_facet_request(request: CatalogFacetInput) -> CatalogFacetInput:
    data = request.model_dump(mode="python")
    query = request.query.lower()
    inferred_facet = _infer_facet_from_text(query)
    if inferred_facet:
        data["facet"] = inferred_facet
    if not request.category:
        if category := _infer_category_from_text(query):
            data["category"] = category
    if not request.brand:
        if brand := _infer_brand_from_text(query):
            data["brand"] = brand
    if not request.spec_key:
        if spec_key := _infer_spec_key_from_text(query):
            data["spec_key"] = spec_key
            if inferred_facet is None and request.facet == "brand":
                data["facet"] = "spec_value"
    if data.get("category") == "speaker" and _looks_like_power_facet_query(query):
        data["facet"] = "spec_value"
        data["spec_key"] = "power_w"
    return CatalogFacetInput.model_validate(data)


def _infer_facet_from_text(query: str) -> str | None:
    if _asks_for_spec_values(query):
        return "spec_value"
    if _asks_for_spec_keys(query):
        return "spec_key"
    if _asks_for_categories(query):
        return "category"
    if _asks_for_brands(query):
        return "brand"
    return None


def _asks_for_brands(query: str) -> bool:
    brand_terms = {
        "brand",
        "brands",
        "maker",
        "manufacturers",
        "牌子",
        "品牌",
    }
    return any(term in query for term in brand_terms)


def _asks_for_categories(query: str) -> bool:
    category_terms = {
        "category",
        "categories",
        "type",
        "types",
        "product line",
        "peripheral",
        "peripherals",
        "品类",
        "类目",
        "类型",
        "外设",
    }
    return any(term in query for term in category_terms) and not _asks_for_spec_values(query)


def _asks_for_spec_keys(query: str) -> bool:
    spec_key_terms = {
        "spec",
        "specs",
        "specification",
        "specifications",
        "parameter",
        "parameters",
        "filter",
        "filters",
        "规格",
        "参数",
        "筛选",
    }
    return any(term in query for term in spec_key_terms) and not _asks_for_spec_values(query)


def _asks_for_spec_values(query: str) -> bool:
    value_terms = {
        "available",
        "values",
        "options",
        "哪些",
        "可选",
        "有什么",
        "有哪",
    }
    return _infer_spec_key_from_text(query) is not None and any(
        term in query for term in value_terms
    )


def _looks_like_power_facet_query(query: str) -> bool:
    power_terms = {"power", "watt", "wattage", "功率", "瓦数", "多少w", "多少瓦"}
    value_terms = {"available", "values", "options", "哪些", "档位", "可选", "有什么", "有哪"}
    return any(term in query for term in power_terms) and any(term in query for term in value_terms)


def _infer_category_from_text(query: str) -> str | None:
    for term in CATEGORY_FILTERS:
        if term in query:
            return term
    for raw in sorted(CATEGORY_ALIASES, key=len, reverse=True):
        if raw.lower() in query:
            return _canonical_category(raw)
    return None


BRAND_ALIASES = {
    "罗技": "Logitech",
    "logi": "Logitech",
    "雷蛇": "Razer",
    "赛睿": "SteelSeries",
    "steelseries": "SteelSeries",
    "脉冲星": "Pulsar",
    "凯酷": "Keychron",
    "键盘侠": "Keychron",
    "艾酷": "Akko",
    "艾石头": "Akko",
    "wooting": "Wooting",
    "极度未知": "HyperX",
    "金士顿": "HyperX",
    "索尼": "Sony",
    "冠捷": "AOC",
    "华硕": "ASUS",
    "戴尔": "Dell",
    "乐金": "LG",
    "漫步者": "Edifier",
    "博士": "Bose",
    "bose": "Bose",
    "创新": "Creative",
    "爱乐图": "Elgato",
    "圆刚": "AVerMedia",
    "圆展": "AVerMedia",
}


KNOWN_BRANDS = (
    "Logitech",
    "Razer",
    "SteelSeries",
    "Pulsar",
    "Keychron",
    "Akko",
    "Wooting",
    "HyperX",
    "Sony",
    "AOC",
    "ASUS",
    "Dell",
    "LG",
    "Edifier",
    "JBL",
    "Bose",
    "Creative",
    "Elgato",
    "AVerMedia",
)


def _infer_brand_from_text(query: str) -> str | None:
    brands = _infer_brands_from_text(query)
    return brands[0] if brands else None


def _infer_spec_key_from_text(query: str) -> str | None:
    aliases = {
        "refresh_rate": {"refresh", "hz", "刷新率"},
        "resolution": {"resolution", "2k", "4k", "分辨率"},
        "switches": {"switch", "switches", "axis", "轴", "轴体"},
        "connection_type": {
            "wireless",
            "wired",
            "connection",
            "无线",
            "有线",
            "连接",
        },
        "max_dpi": {"dpi"},
        "color": {"color", "colour", "颜色"},
    }
    for key, terms in aliases.items():
        if any(term in query for term in terms):
            return key
    return None
