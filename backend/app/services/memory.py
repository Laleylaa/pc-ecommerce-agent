import re
from copy import deepcopy
from typing import Any

from app.schemas.catalog import ProductCard, ProductSearchRequest
from app.schemas.chat import EvidenceItem

PRODUCT_FOLLOWUP_TERMS = [
    "换成",
    "换个",
    "换一款",
    "无线",
    "有线",
    "红轴",
    "青轴",
    "rgb",
    "麦克风",
    "带麦",
    "便宜",
    "贵一点",
    "这款",
    "第二个",
    "第一个",
    "第三个",
]
ORDER_REFERENCE_TERMS = ["这个订单", "这笔订单", "刚才的订单", "上一单", "这单"]
POLICY_REFERENCE_TERMS = [
    "这个政策",
    "该政策",
    "这个规则",
    "这条规则",
    "那保修",
    "还有呢",
]
STABLE_MEMORY_MARKERS = ["以后", "长期", "我通常", "请记住", "记住我"]
ORDINAL_PRODUCT_REFERENCES = [
    (0, ["第一个", "第一款", "第1个", "第1款", "1号"]),
    (1, ["第二个", "第二款", "第2个", "第2款", "2号"]),
    (2, ["第三个", "第三款", "第3个", "第3款", "3号"]),
    (3, ["第四个", "第四款", "第4个", "第4款", "4号"]),
    (4, ["第五个", "第五款", "第5个", "第5款", "5号"]),
    (5, ["第六个", "第六款", "第6个", "第6款", "6号"]),
]


class MemoryService:
    def normalize_working_memory(self, value: dict[str, Any] | None) -> dict[str, Any]:
        if not value:
            return {}
        return deepcopy(value)

    def resolve_intent(
        self, message: str, intent: str, working_memory: dict[str, Any]
    ) -> str:
        if intent == "general" and self._is_product_followup(message, working_memory):
            return "product_recommendation"
        if intent == "general" and self._is_policy_followup(message, working_memory):
            return "after_sales"
        return intent

    def resolve_product_search(
        self,
        message: str,
        search: ProductSearchRequest,
        working_memory: dict[str, Any],
    ) -> ProductSearchRequest:
        previous = working_memory.get("current_product_search")
        if not previous or not self._is_product_followup(message, working_memory):
            return search

        previous_search = ProductSearchRequest.model_validate(previous)
        filters = {**previous_search.filters, **search.filters}
        min_price = search.min_price if search.min_price is not None else previous_search.min_price
        max_price = search.max_price if search.max_price is not None else previous_search.max_price
        return ProductSearchRequest(
            query=search.query if search.category else previous_search.query,
            category=search.category or previous_search.category,
            min_price=min_price,
            max_price=max_price,
            filters=filters,
            limit=search.limit or previous_search.limit,
        )

    def resolve_order_id(
        self,
        message: str,
        explicit_order_id: int | None,
        working_memory: dict[str, Any],
    ) -> int | None:
        if explicit_order_id is not None:
            return explicit_order_id
        if not any(term in message for term in ORDER_REFERENCE_TERMS):
            return None
        value = working_memory.get("last_order_id")
        return int(value) if value is not None else None

    def resolve_referenced_product(
        self, message: str, working_memory: dict[str, Any]
    ) -> dict[str, Any] | None:
        recent_products = working_memory.get("recent_products") or []
        if not recent_products:
            return None

        for index, markers in ORDINAL_PRODUCT_REFERENCES:
            if any(marker in message for marker in markers):
                if index < len(recent_products):
                    return deepcopy(recent_products[index])
                return None

        if any(term in message for term in ["这款", "这个", "上面那个", "刚才那个"]):
            last_referenced = working_memory.get("last_referenced_product")
            if last_referenced:
                return deepcopy(last_referenced)
            return deepcopy(recent_products[0])

        return None

    def resolve_knowledge_query(self, message: str, working_memory: dict[str, Any]) -> str:
        last_policy_query = working_memory.get("last_policy_query")
        if not last_policy_query:
            return message
        if self._is_controlled_policy_followup(message):
            return f"{last_policy_query}\n追问：{message}"
        return message

    def update_after_turn(
        self,
        working_memory: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        next_memory = deepcopy(working_memory)
        intent = state.get("intent")

        if intent == "product_recommendation":
            product_search = state.get("parsed", {}).get("product_search")
            if product_search:
                next_memory["current_product_search"] = product_search
            products = state.get("products") or []
            if products:
                next_memory["recent_products"] = [
                    _product_memory_item(product) for product in products[:6]
                ]

        order = state.get("order")
        if intent == "order_status" and order is not None:
            next_memory["last_order_id"] = order.id

        evidence = state.get("evidence") or []
        if evidence:
            next_memory["last_policy_query"] = state.get("message", "")
            next_memory["recent_evidence"] = [
                _evidence_memory_item(item) for item in evidence[:3]
            ]

        if state.get("boundary", {}).get("classification") == "human_handoff_required":
            next_memory["pending_handoff"] = self.build_handoff_draft(
                state.get("message", ""),
                next_memory,
                payload_style="snake",
            )

        referenced_product = state.get("parsed", {}).get("referenced_product")
        if referenced_product:
            next_memory["last_referenced_product"] = referenced_product

        return next_memory

    def build_handoff_draft(
        self,
        message: str,
        working_memory: dict[str, Any],
        payload_style: str = "camel",
    ) -> dict[str, Any]:
        order_id = working_memory.get("last_order_id")
        request_type = _infer_handoff_request_type(message)
        if payload_style == "snake":
            return {
                "order_id": order_id,
                "request_type": request_type,
                "reason": message,
            }
        return {
            "orderId": order_id,
            "requestType": request_type,
            "reason": message,
        }

    def extract_long_term_facts(self, message: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for clause in _stable_memory_clauses(message):
            facts.extend(_extract_clause_facts(clause))
        return list({fact["key"]: fact for fact in facts}.values())

    def _is_product_followup(self, message: str, working_memory: dict[str, Any]) -> bool:
        return bool(working_memory.get("current_product_search")) and any(
            term in message for term in PRODUCT_FOLLOWUP_TERMS
        )

    def _is_policy_followup(self, message: str, working_memory: dict[str, Any]) -> bool:
        return bool(working_memory.get("last_policy_query")) and (
            self._is_controlled_policy_followup(message)
        )

    @staticmethod
    def _is_controlled_policy_followup(message: str) -> bool:
        return "保修" in message or any(term in message for term in POLICY_REFERENCE_TERMS)


def _extract_clause_facts(message: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    lowered = message.lower()

    if "无线" in message and _is_negative_preference(message, "无线"):
        facts.append(
            _memory_fact(
                "connection_preference",
                "不偏好无线设备",
                0.8,
                {
                    "preference": "wireless",
                    "negated": True,
                    "operation": "exclude",
                },
            )
        )
    elif "有线" in message and _is_negative_preference(message, "有线"):
        facts.append(
            _memory_fact(
                "connection_preference",
                "不偏好有线设备",
                0.8,
                {
                    "preference": "wired",
                    "negated": True,
                    "operation": "exclude",
                },
            )
        )
    elif "无线" in message:
        facts.append(
            _memory_fact(
                "connection_preference",
                "偏好无线设备",
                0.8,
                {
                    "preference": "wireless",
                    "negated": False,
                    "operation": "set",
                },
            )
        )
    elif "有线" in message:
        facts.append(
            _memory_fact(
                "connection_preference",
                "偏好有线设备",
                0.75,
                {
                    "preference": "wired",
                    "negated": False,
                    "operation": "set",
                },
            )
        )

    budget = _extract_budget_preference(message)
    if budget:
        amount = _extract_budget_amount(message)
        facts.append(
            _memory_fact(
                "budget_preference",
                budget,
                0.7,
                {
                    "amount": amount,
                    "currency": "CNY",
                    "maximum": True,
                    "operation": "set",
                },
            )
        )

    if "fps" in lowered or "游戏" in message:
        negated = _is_negative_preference(message, "fps") or _is_negative_preference(
            message, "游戏"
        )
        facts.append(
            _memory_fact(
                "usage_preference",
                "不偏好游戏场景" if negated else "偏好游戏场景",
                0.75,
                {
                    "usage": "gaming",
                    "negated": negated,
                    "operation": "exclude" if negated else "set",
                },
            )
        )
    elif "办公" in message:
        negated = _is_negative_preference(message, "办公")
        facts.append(
            _memory_fact(
                "usage_preference",
                "不偏好办公场景" if negated else "偏好办公场景",
                0.7,
                {
                    "usage": "office",
                    "negated": negated,
                    "operation": "exclude" if negated else "set",
                },
            )
        )

    brand = _extract_brand_preference(message)
    if brand:
        negated = _is_negative_preference(message, brand)
        facts.append(
            _memory_fact(
                "brand_preference",
                f"不偏好 {brand} 品牌" if negated else f"偏好 {brand} 品牌",
                0.65,
                {
                    "brand": brand,
                    "negated": negated,
                    "operation": "exclude" if negated else "set",
                },
            )
        )

    return facts


def _product_memory_item(product: ProductCard) -> dict[str, Any]:
    return {
        "spu_id": product.spu_id,
        "sku_id": product.sku_id,
        "title": product.title,
        "category": product.category,
        "price": str(product.price),
        "stock": product.stock,
        "specs": product.specs,
    }


def _evidence_memory_item(item: EvidenceItem | dict[str, Any]) -> dict[str, Any]:
    evidence = item if isinstance(item, EvidenceItem) else EvidenceItem(**item)
    return {
        "source_type": evidence.source_type,
        "source_id": evidence.source_id,
        "title": evidence.title,
        "document_type": evidence.document_type,
    }


def _infer_handoff_request_type(message: str) -> str:
    if "退款" in message:
        return "refund"
    if "退货" in message or "退" in message:
        return "return"
    if "维修" in message or "保修" in message:
        return "repair"
    if any(term in message for term in ["取消订单", "改地址", "改收货", "修改订单"]):
        return "order_change"
    return "other"


def _memory_fact(
    key: str, value: str, confidence: float, value_json: dict[str, Any]
) -> dict[str, Any]:
    return {
        "scope": "user",
        "fact_type": "preference",
        "key": key,
        "value": value,
        "value_json": value_json,
        "confidence": confidence,
    }


def _extract_budget_preference(message: str) -> str | None:
    match = re.search(
        r"预算\s*(\d+(?:\.\d+)?)\s*(?:元|块)?|(\d+(?:\.\d+)?)\s*(?:元|块)?以内",
        message,
    )
    if match is None:
        return None
    amount = match.group(1) or match.group(2)
    if amount is None:
        return None
    return f"偏好 {amount} 元以内预算"


def _extract_budget_amount(message: str) -> float:
    match = re.search(
        r"预算\s*(\d+(?:\.\d+)?)\s*(?:元|块)?|(\d+(?:\.\d+)?)\s*(?:元|块)?以内",
        message,
    )
    if match is None:
        raise ValueError("budget amount is unavailable")
    return float(match.group(1) or match.group(2))


def _is_negative_preference(message: str, preference: str) -> bool:
    normalized_message = message.lower()
    normalized_preference = preference.lower()
    return bool(
        re.search(
            rf"(?:不要|不玩|不用|不喜欢|不偏好|排除|别(?:用|要|玩)?)[^，。；]{{0,8}}{re.escape(normalized_preference)}",
            normalized_message,
        )
    )


def _stable_memory_clauses(message: str) -> list[str]:
    clauses = [
        item.strip()
        for item in re.split(r"[，,。.!！？；;\n]|但(?:是)?(?=这次|本次|当前)", message)
    ]
    return [
        clause
        for clause in clauses
        if clause and any(marker in clause for marker in STABLE_MEMORY_MARKERS)
    ]


def _extract_brand_preference(message: str) -> str | None:
    brand_aliases = {
        "罗技": "罗技",
        "logitech": "Logitech",
        "雷蛇": "雷蛇",
        "razer": "Razer",
        "steelseries": "SteelSeries",
        "赛睿": "赛睿",
    }
    lowered = message.lower()
    for keyword, brand in brand_aliases.items():
        if keyword in lowered or keyword in message:
            return brand
    return None
