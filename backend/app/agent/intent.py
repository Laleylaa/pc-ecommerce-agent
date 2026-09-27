import re
from decimal import Decimal

from app.agent.boundary import (
    BOUNDARY_MESSAGES,
    BOUNDARY_POLICY,
    CATEGORY_KEYWORDS,
    BoundaryPolicy,
    _is_purchase_guidance,
    boundary_for_classification,
    classify_boundary,
    requires_security_refusal,
    requires_static_handoff,
)
from app.schemas.catalog import ProductSearchRequest


def classify_intent(message: str) -> str:
    lowered = message.lower()
    compact = re.sub(r"\s+", "", lowered)
    if any(
        keyword in message
        for keyword in ["退货", "换货", "退款", "维修", "售后", "工单"]
    ):
        return "after_sales"
    purchase_history = any(
        marker in compact
        for marker in ("买过", "购买过", "下单过", "订单里有", "哪个订单里", "哪些订单里")
    ) or bool(
        re.search(r"(?:以前|之前|曾经|上次|最近一次).{0,8}(?:买|购买|下单)", compact)
        or re.search(r"(?:有没有|是否).{0,8}(?:买|购买|下单)", compact)
        or re.search(r"(?:买|购买|下单).{0,5}(?:了吗|没有|记录|历史)", compact)
    )
    if purchase_history:
        return "order_status"
    if any(keyword in message for keyword in ["订单", "物流", "快递", "发货"]) or re.search(
        r"\b\d{8,}\b", lowered
    ):
        return "order_status"
    if _is_purchase_guidance(compact):
        return "purchase_guidance"
    if any(keyword in lowered for keyword in CATEGORY_KEYWORDS) or any(
        keyword in message for keyword in ["推荐", "预算", "买", "选", "对比"]
    ):
        return "product_recommendation"
    return "general"


def extract_order_id(message: str) -> int | None:
    match = re.search(r"\b(\d{8,})\b", message)
    if match:
        return int(match.group(1))
    return None


def build_product_search(message: str) -> ProductSearchRequest:
    lowered = message.lower()
    category = None
    for keyword, mapped in CATEGORY_KEYWORDS.items():
        if keyword in lowered or keyword in message:
            category = mapped
            break

    max_price = None
    budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|以内|以下|预算)", message)
    if budget_match:
        max_price = Decimal(budget_match.group(1))

    filters: dict[str, str] = {}
    if "无线" in message or "wireless" in lowered:
        filters["connection_type"] = "Wireless"
    elif "有线" in message or "wired" in lowered:
        filters["connection_type"] = "Wired"
    if "rgb" in lowered:
        filters["backlit"] = "RGB"
    if "红轴" in message:
        filters["switches"] = "Red"
    if "青轴" in message:
        filters["switches"] = "Blue"
    if any(keyword in message for keyword in ["麦克风", "带麦"]):
        filters["microphone"] = "是"

    query = message
    for word in [
        "推荐",
        "预算",
        "以内",
        "以下",
        "我想买",
        "买",
        "选",
        "怎么",
        "哪款",
        "哪个",
        "对比",
        "比较",
    ]:
        query = query.replace(word, " ")
    query = re.sub(r"\d+(?:\.\d+)?\s*(元|块)?", " ", query).strip()

    return ProductSearchRequest(
        query=query if len(query) > 1 else "",
        category=category,
        max_price=max_price,
        filters=filters,
        limit=6,
    )


__all__ = [
    "BOUNDARY_MESSAGES",
    "BOUNDARY_POLICY",
    "BoundaryPolicy",
    "boundary_for_classification",
    "build_product_search",
    "classify_boundary",
    "classify_intent",
    "extract_order_id",
    "requires_security_refusal",
    "requires_static_handoff",
]
