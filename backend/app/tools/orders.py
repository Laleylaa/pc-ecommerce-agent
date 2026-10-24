import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm_calls import ModelCallError
from app.core.llm_feedback import OutputValidationError
from app.repositories.orders import OrderRepository
from app.schemas.order import (
    OrderCard,
    OrderConditionDifference,
    OrderFilters,
    OrderQueryMode,
    OrderSuggestion,
)
from app.tools.order_identity import explicit_order_ids
from app.tools.schemas import (
    OrderLookupInput,
    OrderLookupOutput,
    OrderSummary,
    ToolDiagnostic,
)
from app.tools.structured_planning import generate_tool_plan

DEFAULT_RECENT_ORDER_LIMIT = 5
MAX_ORDER_WINDOW = 5

OrderSelection = Literal["explicit", "latest", "recent", "all", "remaining"]
OrderOperation = Literal["total_count", "list", "detail", "match_orders"]
OrderResultShape = Literal[
    "count",
    "summary",
    "single_detail",
    "analysis_detail",
    "unsupported",
    "invalid_query",
]

SUMMARY_ORDER_FIELDS = frozenset(
    {
        "order_id",
        "order_status",
        "total_amount",
        "pay_amount",
        "created_at",
        "item_count",
        "first_item_name",
        "express_company",
        "logistic_no",
        "latest_logistics_event",
    }
)
DETAIL_ORDER_FIELDS = frozenset(
    {
        *SUMMARY_ORDER_FIELDS,
        "freight_amount",
        "pay_at",
        "delivery_at",
        "order_items",
        "item_name",
        "item_specs",
        "item_price",
        "item_quantity",
        "logistics_status_code",
        "logistics_trace",
    }
)
SUPPORTED_ORDER_FIELDS = frozenset({"order_count", *DETAIL_ORDER_FIELDS})
ORDER_CAPABILITY_EXPANSIONS = {
    "order_count": frozenset({"order_count"}),
    "order_summary": SUMMARY_ORDER_FIELDS,
    "order_detail": DETAIL_ORDER_FIELDS,
    "order_items": frozenset(
        {
            "order_items",
            "item_name",
            "item_specs",
            "item_price",
            "item_quantity",
        }
    ),
    "order_shipping_info": frozenset(
        {
            "express_company",
            "logistic_no",
            "latest_logistics_event",
        }
    ),
    "purchase_history_match": frozenset(
        {"order_id", "created_at", "order_items", "item_name", "item_specs"}
    ),
}


class OrderQueryPlan(BaseModel):
    """Language-level order intent. Capability is decided only by the validator."""

    model_required_fields: ClassVar[tuple[str, ...]] = (
        "selection",
        "operation",
        "requested_fields",
        "filters",
        "unresolved_requirements",
    )
    filters: OrderFilters = Field(default_factory=OrderFilters)
    filter_evidence: list[str] = Field(
        default_factory=list, description="Optional audit notes; not executable filter evidence.",
    )
    unresolved_requirements: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    query: str = ""
    selection: OrderSelection
    operation: OrderOperation
    requested_fields: list[str] = Field(min_length=1, max_length=24)
    limit: int = Field(default=DEFAULT_RECENT_ORDER_LIMIT, ge=1, le=MAX_ORDER_WINDOW)
    offset: int = Field(default=0, ge=0)
    planner: str = "deterministic"
    fallback_reason: str | None = None

    @field_validator("requested_fields")
    @classmethod
    def normalize_requested_fields(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            field_name = re.sub(r"[^a-z0-9_]+", "_", value.strip().casefold()).strip("_")
            if field_name and field_name not in normalized:
                normalized.append(field_name)
        if not normalized:
            raise ValueError("requested_fields cannot be empty")
        return normalized


@dataclass(frozen=True)
class ValidatedOrderQueryPlan:
    plan: OrderQueryPlan
    result_shape: OrderResultShape
    query_mode: OrderQueryMode
    supported_fields: tuple[str, ...]
    unsupported_fields: tuple[str, ...]


class OrderQueryPlanner(Protocol):
    async def plan(self, request: OrderLookupInput) -> OrderQueryPlan: ...


class LLMOrderQueryPlanner:
    """Map natural language to a small order plan without deciding capability."""

    def __init__(self, chat_model, *, review: bool = True):
        self.chat_model = chat_model
        self.review = review

    async def plan(self, request: OrderLookupInput) -> OrderQueryPlan:
        payload = {
            "query": request.query or "",
            "limit": request.limit,
            "trusted_order_id_present": request.order_id is not None,
            "trusted_offset": request.offset,
        }
        payload["current_time"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        payload["timezone"] = "Asia/Shanghai"

        def validate(plan: OrderQueryPlan) -> dict:
            assessed = validate_order_query_plan(
                plan, order_id=request.order_id or _extract_order_id(request.query)
            )
            return {"result_shape": assessed.result_shape,
                    "unsupported_fields": list(assessed.unsupported_fields)}

        return await generate_tool_plan(
            self.chat_model,
            schema=OrderQueryPlan,
            required=OrderQueryPlan.model_required_fields,
            prompt=_order_planner_system_prompt(),
            payload=payload,
            validate=validate,
            review=self.review,
            trusted={
                "query": request.query or "",
                "limit": request.limit,
                "offset": request.offset,
                "planner": "llm",
            },
        )


class OrderToolService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        planner: OrderQueryPlanner | None = None,
    ):
        self.session = session
        self.planner = planner

    async def lookup(self, request: OrderLookupInput) -> OrderLookupOutput:
        order_id = (
            request.order_id if request.order_id is not None else _extract_order_id(request.query)
        )
        fast_plan = _deterministic_fast_plan(request, order_id=order_id)
        if fast_plan is not None:
            plan = fast_plan
        elif self.planner is None:
            return _planning_failed_output(
                "The deterministic order planner could not confidently classify the request, "
                "and no LLM order planner is configured."
            )
        else:
            try:
                plan = await self.planner.plan(request)
            except ModelCallError:
                # Preserve the terminal model error code for the formal tool wrapper.
                raise
            except Exception as exc:
                return _planning_failed_output(str(exc))

        plan = _apply_trusted_order_overrides(plan, request, order_id=order_id)
        try:
            validated = validate_order_query_plan(plan, order_id=order_id)
        except (TypeError, ValueError, ValidationError) as exc:
            return _planning_failed_output(str(exc), plan=plan)

        query_plan = _validated_plan_dump(validated, order_id=order_id)
        diagnostics = _order_plan_diagnostics(validated)
        if validated.result_shape == "invalid_query":
            return OrderLookupOutput(
                result_type="invalid_query",
                query_mode=validated.query_mode,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )
        if validated.result_shape == "unsupported":
            return OrderLookupOutput(
                result_type="unsupported",
                query_mode=validated.query_mode,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )

        repository = OrderRepository(self.session)
        filter_args = {"filters": validated.plan.filters} if validated.plan.filters.active else {}
        if validated.result_shape == "count":
            total_count = await repository.count_orders(request.user_id, **filter_args)
            return OrderLookupOutput(
                result_type="order_count",
                query_mode="count",
                total_match_count=total_count,
                returned_count=0,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )

        if validated.result_shape == "single_detail" and order_id is not None:
            order = await repository.get_order(request.user_id, order_id, **filter_args)
            return OrderLookupOutput(
                result_type="single_order" if order else "not_found",
                order=order,
                focus_order_ids=[order.id] if order else [],
                query_mode="explicit",
                total_match_count=1 if order else 0,
                returned_count=1 if order else 0,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )

        if validated.result_shape == "single_detail":
            total_count = await repository.count_orders(request.user_id, **filter_args)
            order = (
                await repository.latest_order(request.user_id, **filter_args)
                if total_count
                else None
            )
            if not total_count:
                suggestions = await _suggest_orders(repository, request, validated, diagnostics)
                if suggestions is not None:
                    return suggestions
            return OrderLookupOutput(
                result_type="single_order" if order else "not_found",
                order=order,
                focus_order_ids=[order.id] if order else [],
                query_mode="latest",
                total_match_count=total_count,
                returned_count=1 if order else 0,
                is_exhaustive=total_count <= 1,
                next_offset=1 if total_count > 1 else None,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )

        page = await repository.list_recent_orders_page(
            request.user_id,
            limit=validated.plan.limit,
            offset=validated.plan.offset,
            **filter_args,
        )
        if page.total_count == 0:
            suggestions = await _suggest_orders(repository, request, validated, diagnostics)
            if suggestions is not None:
                return suggestions
        returned_count = len(page.orders)
        next_offset = page.offset + returned_count
        is_exhaustive = next_offset >= page.total_count

        if validated.result_shape == "analysis_detail":
            if not page.orders:
                return OrderLookupOutput(
                    result_type="not_found",
                    query_mode="analysis",
                    total_match_count=page.total_count,
                    returned_count=0,
                    is_exhaustive=is_exhaustive,
                    offset=page.offset,
                    query_plan=query_plan,
                    diagnostics=diagnostics,
                )
            return OrderLookupOutput(
                result_type="order_analysis",
                analysis_orders=page.orders,
                focus_order_ids=_analysis_focus_order_ids(validated.plan, page.orders),
                query_mode="analysis",
                total_match_count=page.total_count,
                returned_count=returned_count,
                is_exhaustive=is_exhaustive,
                offset=page.offset,
                next_offset=None if is_exhaustive else next_offset,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )

        if not page.orders:
            if validated.plan.selection == "remaining" and page.total_count > 0:
                return OrderLookupOutput(
                    result_type="order_candidates",
                    query_mode="remaining",
                    total_match_count=page.total_count,
                    returned_count=0,
                    is_exhaustive=True,
                    offset=page.offset,
                    query_plan=query_plan,
                    diagnostics=diagnostics,
                )
            return OrderLookupOutput(
                result_type="not_found",
                query_mode=validated.query_mode,
                total_match_count=page.total_count,
                returned_count=0,
                is_exhaustive=is_exhaustive,
                offset=page.offset,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )
        return OrderLookupOutput(
            result_type="order_candidates",
            candidates=[_summary(order) for order in page.orders],
            focus_order_ids=[order.id for order in page.orders],
            query_mode=validated.query_mode,
            total_match_count=page.total_count,
            returned_count=returned_count,
            is_exhaustive=is_exhaustive,
            offset=page.offset,
            next_offset=None if is_exhaustive else next_offset,
            query_plan=query_plan,
            diagnostics=diagnostics,
        )


async def _suggest_orders(
    repository: OrderRepository,
    request: OrderLookupInput,
    validated: ValidatedOrderQueryPlan,
    diagnostics: list[ToolDiagnostic],
) -> OrderLookupOutput | None:
    """After zero exact matches, relax one dimension, retaining the purchased product.

    At most two additional reads. Identity, product terms, unresolved requirements,
    explicit IDs and counts are never relaxed. Suggestions require user confirmation.
    """
    plan = validated.plan
    original = plan.filters
    if (
        request.order_id is not None or _extract_order_id(request.query) is not None
        or plan.offset or not original.product_terms or plan.unresolved_requirements
        or plan.operation == "total_count"
    ):
        return None
    variants: list[tuple[str, OrderFilters]] = []
    if original.start_at is not None or original.end_at is not None:
        variants.append((
            "time_range", original.model_copy(update={"start_at": None, "end_at": None}),
        ))
    if original.statuses:
        variants.append(("statuses", original.model_copy(update={"statuses": []})))
    for field, filters in variants:
        page = await repository.list_recent_orders_page(
            request.user_id, limit=plan.limit, offset=0, filters=filters,
        )
        if not page.orders:
            continue
        suggestions = []
        for order in page.orders:
            if field == "time_range":
                timestamp = getattr(order, original.time_field)
                difference = OrderConditionDifference(
                    field="time_range",
                    expected={
                        "time_field": original.time_field,
                        "start_at": original.start_at.isoformat() if original.start_at else None,
                        "end_at": original.end_at.isoformat() if original.end_at else None,
                    },
                    actual=timestamp.isoformat() if timestamp else None,
                    state="not_matched" if timestamp is not None else "unknown",
                )
            else:
                difference = OrderConditionDifference(
                    field="statuses", expected={"statuses": original.statuses},
                    actual=order.status, state="not_matched",
                )
            suggestions.append(OrderSuggestion(order=order, differences=[difference]))
        return OrderLookupOutput(
            result_type="order_suggestions", query_mode=validated.query_mode,
            suggestions=suggestions, suggestion_filters=filters,
            suggestion_total_count=page.total_count,
            suggestion_is_exhaustive=len(page.orders) >= page.total_count,
            query_plan=_validated_plan_dump(validated, order_id=None),
            diagnostics=[*diagnostics, ToolDiagnostic(
                code="order_confirmation_required", severity="warning",
                message="No exact match. These orders require user confirmation before selection.",
                recommended_action="ask_user_to_confirm_order",
                details={"relaxed_condition": field},
            )],
        )
    return None


def validate_order_query_plan(
    plan: OrderQueryPlan | dict[str, Any],
    *,
    order_id: int | None,
) -> ValidatedOrderQueryPlan:
    if isinstance(plan, dict):
        plan = OrderQueryPlan.model_validate(plan)
    if plan.selection == "explicit" and order_id is None:
        raise OutputValidationError(
            "explicit order selection requires a trusted order id", path=("selection",)
        )
    if plan.selection == "remaining" and plan.offset <= 0:
        raise OutputValidationError(
            "remaining order selection requires trusted continuation context", path=("selection",)
        )
    if (
        plan.operation == "match_orders"
        and not plan.filters.product_terms
        and not plan.unresolved_requirements
    ):
        raise OutputValidationError("Purchase matching requires product filters")

    expanded_fields: set[str] = set()
    for field_name in plan.requested_fields:
        expanded_fields.update(ORDER_CAPABILITY_EXPANSIONS.get(field_name, frozenset({field_name})))
    supported_fields = tuple(sorted(expanded_fields & SUPPORTED_ORDER_FIELDS))
    unsupported_fields = tuple(sorted(expanded_fields - SUPPORTED_ORDER_FIELDS))

    if plan.filters.conflicting:
        result_shape: OrderResultShape = "invalid_query"
    elif not supported_fields or plan.unresolved_requirements:
        result_shape: OrderResultShape = "unsupported"
    elif plan.operation == "total_count":
        result_shape = "count"
    elif plan.selection in {"explicit", "latest"}:
        result_shape = "single_detail"
    elif plan.operation in {"detail", "match_orders"} or not set(supported_fields).issubset(
        SUMMARY_ORDER_FIELDS
    ):
        result_shape = "analysis_detail"
    else:
        result_shape = "summary"

    query_mode: OrderQueryMode
    if result_shape == "count":
        query_mode = "count"
    elif result_shape == "analysis_detail":
        query_mode = "analysis"
    else:
        query_mode = plan.selection

    return ValidatedOrderQueryPlan(
        plan=plan,
        result_shape=result_shape,
        query_mode=query_mode,
        supported_fields=supported_fields,
        unsupported_fields=unsupported_fields,
    )


def _deterministic_fast_plan(
    request: OrderLookupInput,
    *,
    order_id: int | None,
) -> OrderQueryPlan | None:
    query = re.sub(r"\s+", "", request.query or "")
    if not query:
        return _new_plan(
            query,
            selection="recent",
            operation="list",
            requested_fields=["order_summary"],
            limit=request.limit,
        )

    # Closed grammars only: every part must be consumed. All richer language,
    # including dates, statuses and product constraints, goes to the LLM.
    if _is_plain_recent_query(query):
        return _new_plan(
            query,
            selection="recent",
            operation="list",
            requested_fields=["order_summary"],
            limit=request.limit,
        )
    if order_id is not None and _is_plain_explicit_order_query(query, order_id):
        return _new_plan(
            query,
            selection="explicit",
            operation="detail",
            requested_fields=["order_detail"],
            limit=1,
        )
    if re.fullmatch(
        r"(?:请)?(?:查一下|查询|查看|列出)?(?:我)?(?:的)?(?:全部|所有)订单(?:列表|摘要)?[？?。]?",
        query,
    ):
        return _new_plan(
            query,
            selection="all",
            operation="list",
            requested_fields=["order_summary"],
            limit=request.limit,
        )
    if re.fullmatch(r"(?:我)?(?:的)?(?:订单总数|一共有几笔订单|总共有多少笔订单)[？?。]?", query):
        return _new_plan(
            query,
            selection="all",
            operation="total_count",
            requested_fields=["order_count"],
            limit=request.limit,
        )
    return None


def _new_plan(
    query: str,
    *,
    selection: OrderSelection,
    operation: OrderOperation,
    requested_fields: list[str],
    limit: int,
    offset: int = 0,
) -> OrderQueryPlan:
    return OrderQueryPlan(
        query=query,
        selection=selection,
        operation=operation,
        requested_fields=requested_fields,
        limit=max(1, min(limit, MAX_ORDER_WINDOW)),
        offset=offset,
    )


def _apply_trusted_order_overrides(
    plan: OrderQueryPlan,
    request: OrderLookupInput,
    *,
    order_id: int | None,
) -> OrderQueryPlan:
    update: dict[str, Any] = {
        "query": request.query or plan.query,
        "limit": request.limit,
        "offset": request.offset,
    }
    if order_id is not None:
        update.update({"selection": "explicit", "limit": 1, "offset": 0})
    elif request.offset > 0:
        update.update({"selection": "remaining", "offset": request.offset})
    return plan.model_copy(update=update)


def _summary(order: OrderCard) -> OrderSummary:
    first_item = order.items[0] if order.items else None
    logistics = order.logistics
    latest_event = None
    if logistics and logistics.trace:
        candidate = logistics.trace[-1]
        latest_event = dict(candidate) if isinstance(candidate, dict) else None
    return OrderSummary(
        id=order.id,
        status=order.status,
        status_label=order.status_label,
        total_amount=order.total_amount,
        pay_amount=order.pay_amount,
        created_at=order.created_at.isoformat(),
        item_count=len(order.items),
        first_item_name=first_item.sku_name if first_item else None,
        express_company=logistics.express_company if logistics else None,
        logistic_no=logistics.logistic_no if logistics else None,
        latest_logistics_event=latest_event,
    )


def _analysis_focus_order_ids(plan: OrderQueryPlan, orders: list[OrderCard]) -> list[int]:
    # Repository filtering is authoritative; never re-interpret the query or
    # combine category/brand matches from different order items after pagination.
    return [order.id for order in orders]


def _extract_order_id(query: str | None) -> int | None:
    if not query:
        return None
    matches = explicit_order_ids(query)
    if len(matches) != 1:
        return None
    return next(iter(matches))


def _is_plain_explicit_order_query(query: str, order_id: int) -> bool:
    without_id = query.replace(str(order_id), "").casefold()
    if re.fullmatch(
        r"(?:please)?(?:check|show|find|lookup|lookat)?(?:my)?(?:order)?",
        without_id,
    ):
        return True
    return bool(
        re.fullmatch(
            r"(?:请)?(?:帮我)?(?:查|查一下|查询|看|看看)?(?:一下)?(?:订单|这笔订单|这单)?",
            without_id,
        )
    )


def _is_plain_recent_query(query: str) -> bool:
    compact = re.sub(r"[，。！？、,.!?]", "", query.casefold())
    if compact in {"showmyrecentorders", "showmyorders", "myorders", "recentorders"}:
        return True
    return bool(
        re.fullmatch(
            r"(?:请)?(?:帮我)?(?:查|查一下|查询|看|看看|列出|列一下)"
            r"(?:我)?(?:的)?(?:最近|最近的)?订单(?:摘要|列表)?",
            compact,
        )
        or re.fullmatch(r"(?:我)?(?:的)?(?:最近|最近的)?订单(?:摘要|列表)?", compact)
    )


def _validated_plan_dump(
    validated: ValidatedOrderQueryPlan,
    *,
    order_id: int | None,
) -> dict[str, Any]:
    return {
        **validated.plan.model_dump(mode="json"),
        "order_id": order_id,
        "result_shape": validated.result_shape,
        "query_mode": validated.query_mode,
        "supported_fields": list(validated.supported_fields),
        "unsupported_fields": list(validated.unsupported_fields),
        "supported": validated.result_shape not in {"unsupported", "invalid_query"},
        "fully_supported": not (
            validated.unsupported_fields
            or validated.plan.unresolved_requirements
            or validated.plan.filters.conflicting
        ),
    }


def _order_plan_diagnostics(
    validated: ValidatedOrderQueryPlan,
) -> list[ToolDiagnostic]:
    if validated.result_shape == "invalid_query":
        return [
            ToolDiagnostic(
                code="conflicting_conditions",
                severity="warning",
                message="The planned interval cannot hold; confirm its start and exclusive end",
                recommended_action="explain_invalid_query",
                details={"filters": validated.plan.filters.model_dump(mode="json")},
            )
        ]
    if validated.result_shape == "unsupported":
        return [
            ToolDiagnostic(
                code="unsupported_query",
                severity="error",
                message="The requested order information is not exposed by the Order Tool.",
                recommended_action="explain_unsupported_query",
                details={
                    "unsupported_fields": list(validated.unsupported_fields),
                    "unresolved_requirements": validated.plan.unresolved_requirements,
                    "conflicting_time_range": validated.plan.filters.conflicting,
                    "available_fields": sorted(SUPPORTED_ORDER_FIELDS),
                },
            )
        ]
    if validated.unsupported_fields:
        return [
            ToolDiagnostic(
                code="partial_unsupported_query",
                severity="warning",
                message=(
                    "Some requested order fields are unavailable; supported fields were queried."
                ),
                recommended_action="use_supported_fields_and_explain_unavailable_fields",
                details={
                    "supported_fields": list(validated.supported_fields),
                    "unsupported_fields": list(validated.unsupported_fields),
                },
            )
        ]
    return []


def _planning_failed_output(
    reason: str,
    *,
    plan: OrderQueryPlan | None = None,
) -> OrderLookupOutput:
    query_plan = plan.model_dump(mode="json") if plan is not None else {}
    query_plan.update({"error_type": "invalid_order_plan", "fallback_reason": reason})
    return OrderLookupOutput(
        result_type="planning_failed",
        query_plan=query_plan,
        diagnostics=[
            ToolDiagnostic(
                code="invalid_order_plan",
                severity="error",
                message="The Order Tool could not safely plan this request.",
                recommended_action="ask_user_to_rephrase",
                details={"reason": reason},
            )
        ],
    )


def _order_planner_system_prompt() -> str:
    from app.tools.prompts.orders import build_order_prompt

    return build_order_prompt(SUPPORTED_ORDER_FIELDS, OrderQueryPlan)
