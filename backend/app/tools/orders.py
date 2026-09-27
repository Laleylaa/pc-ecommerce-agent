import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.orders import OrderRepository
from app.schemas.order import OrderCard, OrderQueryMode
from app.tools.schemas import (
    OrderLookupInput,
    OrderLookupOutput,
    OrderSummary,
    ToolDiagnostic,
)

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

    def __init__(self, chat_model):
        self.chat_model = chat_model

    async def plan(self, request: OrderLookupInput) -> OrderQueryPlan:
        payload = {
            "query": request.query or "",
            "limit": min(request.limit, MAX_ORDER_WINDOW),
            "trusted_order_id_present": request.order_id is not None,
            "trusted_offset": request.offset,
        }
        last_error: Exception | None = None
        for attempt in range(2):
            response = await self.chat_model.ainvoke(
                [
                    SystemMessage(content=_order_planner_system_prompt()),
                    HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                ]
            )
            try:
                plan_data = _extract_json_object(_message_content_to_text(response.content))
                plan_data["query"] = request.query or ""
                plan_data["limit"] = min(
                    int(plan_data.get("limit") or request.limit),
                    MAX_ORDER_WINDOW,
                )
                plan_data["offset"] = request.offset
                plan_data["planner"] = "llm"
                return OrderQueryPlan.model_validate(plan_data)
            except (TypeError, ValueError, ValidationError) as exc:
                last_error = exc
                payload["validation_feedback"] = str(exc)
                payload["retry_instruction"] = (
                    "Return corrected JSON only. Preserve every requested field, including "
                    "requirements outside the available-field list."
                )
                if attempt == 1:
                    break
        raise ValueError(f"LLM order planner failed validation: {last_error}")


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
            request.order_id
            if request.order_id is not None
            else _extract_order_id(request.query)
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
            except Exception as exc:
                return _planning_failed_output(str(exc))

        plan = _apply_trusted_order_overrides(plan, request, order_id=order_id)
        try:
            validated = validate_order_query_plan(plan, order_id=order_id)
        except (TypeError, ValueError, ValidationError) as exc:
            return _planning_failed_output(str(exc), plan=plan)

        query_plan = _validated_plan_dump(validated, order_id=order_id)
        diagnostics = _order_plan_diagnostics(validated)
        if validated.result_shape == "unsupported":
            return OrderLookupOutput(
                result_type="unsupported",
                query_mode=validated.query_mode,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )

        repository = OrderRepository(self.session)
        if validated.result_shape == "count":
            total_count = await repository.count_orders(request.user_id)
            return OrderLookupOutput(
                result_type="order_count",
                query_mode="count",
                total_match_count=total_count,
                returned_count=0,
                query_plan=query_plan,
                diagnostics=diagnostics,
            )

        if validated.result_shape == "single_detail" and order_id is not None:
            order = await repository.get_order(request.user_id, order_id)
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
            total_count = await repository.count_orders(request.user_id)
            order = await repository.latest_order(request.user_id) if total_count else None
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
        )
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


def validate_order_query_plan(
    plan: OrderQueryPlan | dict[str, Any],
    *,
    order_id: int | None,
) -> ValidatedOrderQueryPlan:
    if isinstance(plan, dict):
        plan = OrderQueryPlan.model_validate(plan)
    if plan.selection == "explicit" and order_id is None:
        raise ValueError("explicit order selection requires a trusted order id")
    if plan.selection == "remaining" and plan.offset <= 0:
        raise ValueError("remaining order selection requires trusted continuation context")

    expanded_fields: set[str] = set()
    for field_name in plan.requested_fields:
        expanded_fields.update(
            ORDER_CAPABILITY_EXPANSIONS.get(field_name, frozenset({field_name}))
        )
    supported_fields = tuple(sorted(expanded_fields & SUPPORTED_ORDER_FIELDS))
    unsupported_fields = tuple(sorted(expanded_fields - SUPPORTED_ORDER_FIELDS))

    if not supported_fields:
        result_shape: OrderResultShape = "unsupported"
    elif plan.operation == "total_count":
        result_shape = "count"
    elif plan.selection in {"explicit", "latest"}:
        result_shape = "single_detail"
    elif (
        plan.operation in {"detail", "match_orders"}
        or not set(supported_fields).issubset(SUMMARY_ORDER_FIELDS)
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
            limit=MAX_ORDER_WINDOW,
        )

    fields = _deterministic_requested_fields(query)
    selection, limit = _deterministic_selection(request, query, order_id=order_id)
    asks_for_purchase_time = _asks_for_purchase_time(query)
    asks_for_match = any(
        marker in query
        for marker in (
            "买过",
            "买没买",
            "有没有买",
            "哪笔订单",
            "哪个订单",
            "哪些订单",
            "几次",
            "几笔",
            "最近一次",
        )
    ) or asks_for_purchase_time
    asks_for_list = any(
        marker in query
        for marker in ("列出", "列一下", "列表", "摘要", "全部", "所有", "剩下", "其他订单")
    )
    asks_for_detail = any(
        marker in query
        for marker in ("详情", "完整信息", "全部信息", "订单明细", "商品明细")
    )
    asks_for_count = any(
        marker in query
        for marker in ("多少订单", "多少个订单", "多少笔订单", "订单总数", "一共有几", "总共有几")
    )
    has_count_filter = any(
        marker in query
        for marker in ("已发货", "待付款", "待发货", "已完成", "已关闭", "包含", "买过")
    )

    if asks_for_count and not has_count_filter and not asks_for_list:
        return _new_plan(
            query,
            selection="all",
            operation="total_count",
            requested_fields=["order_count"],
            limit=MAX_ORDER_WINDOW,
        )
    if asks_for_match:
        requested_fields = list(fields)
        if asks_for_purchase_time:
            if "purchase_history_match" not in requested_fields:
                requested_fields.insert(0, "purchase_history_match")
            if "created_at" not in requested_fields:
                requested_fields.append("created_at")
        return _new_plan(
            query,
            selection=selection or "all",
            operation="match_orders",
            requested_fields=requested_fields or ["purchase_history_match"],
            limit=limit,
            offset=request.offset,
        )
    if fields and any(field_name not in SUPPORTED_ORDER_FIELDS for field_name in fields):
        return _new_plan(
            query,
            selection=selection or ("explicit" if order_id is not None else "all"),
            operation="detail" if order_id is not None else "list",
            requested_fields=fields,
            limit=limit,
            offset=request.offset,
        )
    if selection == "remaining":
        return _new_plan(
            query,
            selection="remaining",
            operation="detail" if asks_for_detail else "list",
            requested_fields=fields or ["order_summary"],
            limit=MAX_ORDER_WINDOW,
            offset=request.offset,
        )
    if order_id is not None:
        if fields or asks_for_detail or _is_plain_explicit_order_query(query, order_id):
            return _new_plan(
                query,
                selection="explicit",
                operation="detail",
                requested_fields=fields or ["order_detail"],
                limit=1,
            )
        return None
    if selection == "latest":
        return _new_plan(
            query,
            selection="latest",
            operation="detail",
            requested_fields=fields or ["order_detail"],
            limit=1,
        )
    if selection in {"recent", "all"} and (
        fields
        or asks_for_list
        or asks_for_detail
        or _extract_requested_limit(query) is not None
    ):
        return _new_plan(
            query,
            selection=selection,
            operation="detail" if asks_for_detail else "list",
            requested_fields=fields or ["order_summary"],
            limit=limit,
        )
    if _is_plain_recent_query(query):
        return _new_plan(
            query,
            selection="recent",
            operation="list",
            requested_fields=["order_summary"],
            limit=MAX_ORDER_WINDOW,
        )
    return None


def _deterministic_requested_fields(query: str) -> list[str]:
    fields: list[str] = []

    def add(field_name: str) -> None:
        if field_name not in fields:
            fields.append(field_name)

    if any(marker in query for marker in ("收件人", "收货人", "谁收货")):
        add("receiver_name")
    if any(marker in query for marker in ("手机号", "手机号码", "联系电话", "联系方式")):
        add("receiver_phone")
    if any(
        marker in query
        for marker in ("收货地址", "地址表", "寄到哪里", "寄到哪儿", "送到哪里", "送到哪儿")
    ):
        add("receiver_address")
    if any(
        marker in query
        for marker in (
            "支付方式",
            "怎么付",
            "如何支付",
            "用什么付",
            "什么方式付",
            "微信还是支付宝",
        )
    ):
        add("payment_method")
    if any(marker in query.casefold() for marker in ("导出", "下载", "excel", "csv")):
        add("downloadable_order_export")

    if any(marker in query for marker in ("订单号", "哪笔订单", "哪个订单")):
        add("order_id")
    if "状态" in query or any(marker in query for marker in ("发货了吗", "签收了吗")):
        add("order_status")
    if any(marker in query for marker in ("订单总金额", "订单总额", "应付金额")):
        add("total_amount")
    if any(marker in query for marker in ("实付", "实际付", "付了多少钱")):
        add("pay_amount")
    if "运费" in query:
        add("freight_amount")
    if any(marker in query for marker in ("支付时间", "什么时候付", "几点付")):
        add("pay_at")
    if any(marker in query for marker in ("发货时间", "什么时候发货", "几点发货")):
        add("delivery_at")
    if any(
        marker in query
        for marker in (
            "多久能到",
            "多久到",
            "什么时候到",
            "几天能到",
            "几天到",
            "预计送达",
            "预计到达",
            "预计哪天到",
            "到货时间",
            "送达时间",
            "还要多久",
            "何时送到",
        )
    ):
        add("estimated_delivery_time")
    if any(
        marker in query
        for marker in (
            "下单时间",
            "什么时候下单",
            "哪天下单",
            "购买时间",
            "什么时候购买",
            "何时购买",
        )
    ) or _asks_for_purchase_time(query):
        add("created_at")
    if any(marker in query for marker in ("几件商品", "商品数量", "多少件")):
        add("item_count")
    if any(
        marker in query
        for marker in (
            "商品明细",
            "买了什么",
            "买的东西",
            "哪些商品",
            "什么商品",
            "商品规格",
            "商品单价",
        )
    ):
        add("order_items")
    if any(marker in query for marker in ("快递公司", "哪家快递", "什么快递")):
        add("express_company")
    if any(marker in query for marker in ("物流单号", "快递单号", "运单号")):
        add("logistic_no")
    if any(marker in query for marker in ("完整物流轨迹", "全部物流轨迹", "轨迹列表")):
        add("logistics_trace")
    elif any(
        marker in query
        for marker in ("物流到哪", "快递到哪", "运输到哪", "最新物流", "物流进度", "物流轨迹")
    ):
        add("latest_logistics_event")
    elif "物流" in query:
        add("order_shipping_info")
    if any(marker in query for marker in ("订单详情", "完整信息", "全部信息")):
        add("order_detail")
    return fields


def _asks_for_purchase_time(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "什么时候买",
            "啥时候买",
            "何时买",
            "哪天买",
            "哪日买",
            "几号买",
            "买的时间",
            "购买时间",
            "什么时候购买",
            "何时购买",
        )
    )


def _deterministic_selection(
    request: OrderLookupInput,
    query: str,
    *,
    order_id: int | None,
) -> tuple[OrderSelection | None, int]:
    if request.offset > 0 or any(
        marker in query for marker in ("继续看", "继续列", "剩下的", "其他订单")
    ):
        return "remaining", MAX_ORDER_WINDOW
    if order_id is not None:
        return "explicit", 1
    if any(
        marker in query
        for marker in (
            "最近一笔",
            "最新一笔",
            "最新订单",
            "最新那一单",
            "最新那单",
            "最近那一单",
            "最近那单",
            "上一单",
        )
    ):
        return "latest", 1
    requested_limit = _extract_requested_limit(query)
    if requested_limit is not None:
        return "recent", requested_limit
    if any(marker in query for marker in ("全部订单", "所有订单", "订单全部")):
        return "all", MAX_ORDER_WINDOW
    if "最近" in query:
        return "recent", MAX_ORDER_WINDOW
    return None, MAX_ORDER_WINDOW


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
        "limit": max(1, min(plan.limit, MAX_ORDER_WINDOW)),
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


_ORDER_CATEGORY_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("鼠标", "mouse"), ("鼠标", "mouse")),
    (("键盘", "keyboard"), ("键盘", "keyboard")),
    (("耳机", "耳麦", "headphone", "headset"), ("耳机", "耳麦", "headphone", "headset")),
    (("摄像头", "webcam", "camera"), ("摄像头", "webcam", "camera")),
    (("显示器", "屏幕", "monitor", "display"), ("显示器", "monitor", "display")),
    (("麦克风", "话筒", "microphone"), ("麦克风", "话筒", "microphone")),
    (("音箱", "音响", "speaker"), ("音箱", "音响", "speaker")),
)
_ORDER_BRAND_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("雷蛇", "razer"), ("雷蛇", "razer")),
    (("罗技", "logitech"), ("罗技", "logitech")),
    (("赛睿", "steelseries"), ("赛睿", "steelseries")),
    (("卓威", "zowie"), ("卓威", "zowie")),
    (("海盗船", "corsair"), ("海盗船", "corsair")),
    (("樱桃", "cherry"), ("樱桃", "cherry")),
)


def _analysis_focus_order_ids(plan: OrderQueryPlan, orders: list[OrderCard]) -> list[int]:
    if plan.operation != "match_orders":
        return [order.id for order in orders]
    query = re.sub(r"\s+", "", plan.query.casefold())
    category_needles = _query_match_needles(query, _ORDER_CATEGORY_ALIASES)
    brand_needles = _query_match_needles(query, _ORDER_BRAND_ALIASES)
    if not category_needles and not brand_needles:
        return [order.id for order in orders]

    focused: list[int] = []
    for order in orders:
        names = [item.sku_name.casefold() for item in order.items]
        category_matches = not category_needles or any(
            any(needle in name for needle in category_needles) for name in names
        )
        brand_matches = not brand_needles or any(
            any(needle in name for needle in brand_needles) for name in names
        )
        if category_matches and brand_matches:
            focused.append(order.id)
    return focused


def _query_match_needles(
    query: str,
    aliases: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...],
) -> tuple[str, ...]:
    needles: list[str] = []
    for query_aliases, name_aliases in aliases:
        if any(alias in query for alias in query_aliases):
            needles.extend(name_aliases)
    return tuple(dict.fromkeys(needles))


def _extract_order_id(query: str | None) -> int | None:
    if not query:
        return None
    matches = re.findall(r"(?<!\d)(\d{8,20})(?!\d)", query)
    if len(matches) != 1:
        return None
    return int(matches[0])


def _extract_requested_limit(query: str) -> int | None:
    digit_match = re.search(r"最近(\d{1,2})(?:个|笔|条)?订单", query)
    if digit_match:
        return max(1, min(int(digit_match.group(1)), MAX_ORDER_WINDOW))
    chinese_numbers = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
    }
    chinese_match = re.search(r"最近([一二两三四五])(?:个|笔|条)?订单", query)
    return chinese_numbers.get(chinese_match.group(1)) if chinese_match else None


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
        "supported": bool(validated.supported_fields),
        "fully_supported": not validated.unsupported_fields,
    }


def _order_plan_diagnostics(
    validated: ValidatedOrderQueryPlan,
) -> list[ToolDiagnostic]:
    if validated.result_shape == "unsupported":
        return [
            ToolDiagnostic(
                code="unsupported_query",
                severity="error",
                message="The requested order information is not exposed by the Order Tool.",
                recommended_action="explain_unsupported_query",
                details={
                    "unsupported_fields": list(validated.unsupported_fields),
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
                    "Some requested order fields are unavailable; "
                    "supported fields were queried."
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
    return f"""
You are the internal query planner for a read-only ecommerce Order Tool.
Return exactly one JSON object and no markdown.

Allowed structural fields:
- selection: explicit, latest, recent, all, or remaining
- operation: total_count, list, detail, or match_orders
- requested_fields: a non-empty array of snake_case requirement names
- limit: integer from 1 to {MAX_ORDER_WINDOW}

The Order Tool can expose only these fields:
{", ".join(sorted(SUPPORTED_ORDER_FIELDS))}

Useful supported bundles:
- order_summary: compact order and latest shipping snapshot
- order_detail: all safe fields exposed by one OrderCard
- order_items: purchased item names, specifications, unit prices, and quantities
- order_shipping_info: carrier, tracking number, and latest recorded trace event
- purchase_history_match: inspect order items to find matching purchase-history orders

Mapping examples:
- "这单一共多少钱" -> total_amount
- "我真正付了多少" -> pay_amount
- "这单收运费了吗" -> freight_amount
- "桌面音响我什么时候买的" -> selection=all, operation=match_orders,
  requested_fields=["purchase_history_match", "created_at"]
- "买的是什么版本" -> order_items
- "物流现在到哪一步了" -> latest_logistics_event
- "什么时候发货/哪天交给快递" -> delivery_at
- "以前买过这个鼠标吗/在哪笔订单买的" -> purchase_history_match
- "订单详情/完整信息" -> order_detail

created_at is both the order creation time and the supported answer to when a matching product
was bought. For product-scoped purchase-time questions, always use operation=match_orders and
preserve both purchase_history_match and created_at. Do not invent purchase_date, purchase_time,
order_date, or bought_at for this supported meaning.

Any requested information not listed above is unavailable. Do not omit it and do not map it
to a nearby supported field. Instead, preserve it as a concise snake_case name in
requested_fields. Examples include receiver_address, receiver_phone, payment_method,
coupon_details, invoice_details, warehouse, delivery_proof, estimated_delivery_time,
and downloadable_order_export.

delivery_at is the historical dispatch timestamp (发货时间), not an estimated arrival time.
A logistics trace timestamp is only when that recorded event happened.

Unavailable arrival-estimate semantic family:
- Asking when the parcel will arrive, what day it will be delivered, how many days remain,
  whether it can arrive before a date/event, or asking the planner to predict from current
  progress is always estimated_delivery_time.
- This includes indirect or colloquial requests such as "照这速度还得几天才能收着",
  "周末前能收到不", "哪天能送上门", "赶得上周五吗", and "大概还要等多久".
- Do not reinterpret these as latest_logistics_event, logistics_trace, delivery_at,
  order_status, or order_shipping_info. Those fields describe recorded facts only and cannot
  answer an arrival prediction.
- If a request asks both "现在到哪了" and "还要多久", preserve both
  latest_logistics_event and estimated_delivery_time so the validator can classify it as mixed.

Do not output a supported boolean. Do not decide whether data exists. Do not write SQL.
Preserve every part of mixed requests, including both available and unavailable fields.
An explicit trusted order id or continuation offset is supplied separately and cannot be changed.
""".strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise ValueError("LLM order planner did not return a JSON object") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM order planner JSON must be an object")
    return parsed


def _message_content_to_text(content: Any) -> str:
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
