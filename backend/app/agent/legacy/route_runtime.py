"""Request Router normalization, deterministic guards, and route projections."""

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.boundary import BOUNDARY_POLICY
from app.agent.intent import classify_intent, extract_order_id
from app.agent.legacy.decisions import PlannedToolCall
from app.agent.legacy.prompts import (
    REQUEST_ROUTER_SYSTEM_PROMPT,
    build_request_router_user_prompt,
)
from app.agent.legacy.responses import (
    _is_identity_or_capability_question,
    _is_safe_direct_request,
)
from app.agent.legacy.routing import (
    RequestRoutePlan,
    RoutedSubquery,
    blocked_subqueries,
    tool_planning_subqueries,
)
from app.agent.legacy.state import AgentState
from app.rules.chat_input import match_input_rule, normalized_text
from app.services.memory import MemoryService


def _contextual_intent(
    message: str,
    working_memory: dict[str, Any],
    memory_service: MemoryService,
) -> str:
    intent = memory_service.resolve_intent(
        message,
        classify_intent(message),
        working_memory,
    )
    if intent == "general" and _is_v2_product_followup(message, working_memory):
        return "product_recommendation"
    if intent == "general" and _is_v2_policy_followup(message, working_memory):
        return "after_sales"
    if intent == "general" and _is_v2_order_followup(message, working_memory):
        return "order_status"
    return intent


def _collective_reference_count(message: str) -> int | None:
    match = re.search(
        r"(?:这|那|上述|上面|刚才(?:推荐|提到|列出)?的?)"
        r"\s*(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:款|个|件|种|笔)",
        message,
    )
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        value = int(raw)
        return value if 1 <= value <= 20 else None
    return _parse_context_chinese_number(raw)


def _parse_context_chinese_number(value: str) -> int | None:
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
    if value.count("十") != 1:
        return None
    tens, ones = value.split("十", maxsplit=1)
    result = digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return result if 1 <= result <= 20 else None


def _is_v2_product_followup(message: str, working_memory: dict[str, Any]) -> bool:
    catalog = working_memory.get("catalog")
    if not isinstance(catalog, dict):
        return False
    has_catalog_context = bool(catalog.get("query_plan") or catalog.get("candidate_sku_ids"))
    return has_catalog_context and any(
        term in message
        for term in (
            "换成",
            "换个",
            "不要",
            "排除",
            "避开",
            "不考虑",
            "无线",
            "有线",
            "便宜",
            "贵一点",
            "这款",
            "这个",
            "第一个",
            "第二个",
            "第三个",
        )
    )


def _is_v2_order_followup(message: str, working_memory: dict[str, Any]) -> bool:
    order = working_memory.get("order")
    if not isinstance(order, dict):
        return False
    has_order_context = bool(
        order.get("last_order_id")
        or order.get("candidate_order_ids")
    )
    return has_order_context and (
        _order_ordinal_index(message) is not None
        or any(
            marker in message
            for marker in (
                "多久能到",
                "什么时候到",
                "预计送达",
                "预计到达",
                "到货时间",
                "这个订单",
                "这笔订单",
                "刚才的订单",
                "上一单",
                "这单",
            )
        )
    )


def _fallback_catalog_query(state: AgentState) -> str:
    """Build query-only context when the runtime has no orchestrator LLM."""
    message = state["message"].strip()
    working_memory = state.get("working_memory_snapshot", state.get("working_memory", {}))
    explicit_categories = (
        "鼠标",
        "键盘",
        "耳机",
        "显示器",
        "摄像头",
        "音箱",
        "mouse",
        "keyboard",
        "headset",
        "monitor",
        "webcam",
        "speaker",
    )
    if (
        any(category in message.casefold() for category in explicit_categories)
        or not _is_v2_product_followup(message, working_memory)
    ):
        return message
    catalog = working_memory.get("catalog")
    query_plan = catalog.get("query_plan") if isinstance(catalog, dict) else None
    previous_query = query_plan.get("query") if isinstance(query_plan, dict) else None
    if not isinstance(previous_query, str) or not previous_query.strip():
        return message
    return f"此前商品需求：{previous_query.strip()}；当前补充要求：{message}"


def _is_v2_policy_followup(message: str, working_memory: dict[str, Any]) -> bool:
    policy = working_memory.get("policy")
    if not isinstance(policy, dict) or not policy.get("last_query"):
        return False
    return any(
        term in message
        for term in (
            "这个政策",
            "该政策",
            "这个规则",
            "这条规则",
            "那保修",
            "保修",
            "还有呢",
        )
    )


def _resolve_order_id(
    message: str,
    explicit_order_id: int | None,
    working_memory: dict[str, Any],
    memory_service: MemoryService,
) -> int | None:
    if explicit_order_id is not None:
        return explicit_order_id
    order = working_memory.get("order")
    if isinstance(order, dict):
        candidate_ids = order.get("candidate_order_ids")
        if isinstance(candidate_ids, list):
            ordinal = _order_ordinal_index(message)
            if ordinal is not None and ordinal < len(candidate_ids):
                value = candidate_ids[ordinal]
                if isinstance(value, int):
                    return value
        if any(
            term in message
            for term in (
                "这个订单",
                "这笔订单",
                "刚才的订单",
                "上一单",
                "这单",
                "多久能到",
                "什么时候到",
                "预计送达",
                "预计到达",
                "到货时间",
            )
        ):
            value = order.get("last_order_id")
            if isinstance(value, int):
                return value
    return memory_service.resolve_order_id(message, explicit_order_id, working_memory)


def _order_ordinal_index(message: str) -> int | None:
    digit_match = re.search(r"第\s*(\d{1,2})\s*(?:个|笔|条)?(?:订单)?", message)
    if digit_match:
        value = int(digit_match.group(1))
        return value - 1 if 1 <= value <= 20 else None
    chinese_numbers = {
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
        "十": 10,
        "十一": 11,
        "十二": 12,
        "十三": 13,
        "十四": 14,
        "十五": 15,
        "十六": 16,
        "十七": 17,
        "十八": 18,
        "十九": 19,
        "二十": 20,
    }
    chinese_match = re.search(
        r"第([一二两三四五六七八九十]{1,3})(?:个|笔|条)?(?:订单)?",
        message,
    )
    if chinese_match:
        return chinese_numbers[chinese_match.group(1)] - 1
    return None


def _knowledge_memory_view(working_memory: dict[str, Any]) -> dict[str, Any]:
    policy = working_memory.get("policy")
    if not isinstance(policy, dict) or not policy.get("last_query"):
        return working_memory
    return {**working_memory, "last_policy_query": policy["last_query"]}


def _tool_result_payload(result: Any) -> dict[str, Any]:
    if result.ok and result.output is not None:
        return result.output
    return result.model_dump(mode="json", exclude={"output"})


def _request_router_messages(
    state: AgentState,
    *,
    validation_feedback: dict[str, Any] | None = None,
) -> list[SystemMessage | HumanMessage | AIMessage]:
    messages: list[SystemMessage | HumanMessage | AIMessage] = [
        SystemMessage(content=REQUEST_ROUTER_SYSTEM_PROMPT)
    ]
    for item in state.get("history", []):
        content = item.get("content", "")
        if not content:
            continue
        if item.get("role") == "user":
            messages.append(HumanMessage(content=content))
        elif item.get("role") == "assistant":
            messages.append(AIMessage(content=content))
    messages.append(
        HumanMessage(
            content=build_request_router_user_prompt(
                message=state["message"],
                working_memory=state.get(
                    "working_memory_snapshot", state.get("working_memory", {})
                ),
                explicit_user_preferences=state.get("memory", []),
                validation_feedback=validation_feedback,
            )
        )
    )
    return messages


def _context_retry_feedback(
    plan: RequestRoutePlan,
    working_memory: dict[str, Any],
) -> dict[str, Any] | None:
    """Request one Router re-evaluation when context use violates the Task DAG boundary."""
    catalog = working_memory.get("catalog")
    order = working_memory.get("order")
    catalog = catalog if isinstance(catalog, dict) else {}
    order = order if isinstance(order, dict) else {}
    catalog_display = catalog.get("candidate_display")
    catalog_candidate_count = (
        len(catalog_display)
        if isinstance(catalog_display, list)
        else max(
            len(catalog.get("candidate_sku_ids") or []),
            len(catalog.get("candidate_spu_ids") or []),
        )
    )
    order_candidates = order.get("candidate_order_ids")
    order_candidate_count = (
        len(order_candidates) if isinstance(order_candidates, list) else 0
    )
    has_catalog_reference = bool(
        catalog_candidate_count
        or catalog.get("referenced_sku_id")
        or catalog.get("referenced_spu_id")
    )
    has_order_reference = bool(order_candidate_count or order.get("last_order_id"))
    if not has_catalog_reference and not has_order_reference:
        return None
    if not any(item.disposition == "clarification" for item in plan.subqueries):
        return None
    return {
        "code": "clarification_requires_context_recheck",
        "instruction": (
            "The previous route asked for clarification while trusted working memory contains "
            "possible references. Re-evaluate the request against every available domain. Use "
            "catalog_candidates or order_candidates with one-based positions when the reference "
            "is uniquely resolvable. Keep clarification only if the domain or positions remain "
            "genuinely ambiguous; do not force a context match."
        ),
        "available_context": {
            "catalog_candidate_count": catalog_candidate_count,
            "catalog_selection_scope": (
                catalog.get("query_plan", {}).get("selection_scope")
                if isinstance(catalog.get("query_plan"), dict)
                else None
            ),
            "has_catalog_reference": has_catalog_reference,
            "order_candidate_count": order_candidate_count,
            "has_order_reference": has_order_reference,
        },
        "previous_clarifications": [
            {
                "query": item.query,
                "reason_code": item.reason_code,
                "missing_information": item.missing_information,
            }
            for item in plan.subqueries
            if item.disposition == "clarification"
        ],
    }


def _fallback_rewritten_query(
    state: AgentState,
    memory_service: MemoryService,
) -> str:
    message = " ".join(state["message"].split())
    working_memory = state.get("working_memory_snapshot", state.get("working_memory", {}))
    intent = _contextual_intent(message, working_memory, memory_service)
    if intent == "product_recommendation":
        return _fallback_catalog_query({**state, "message": message})
    if intent == "after_sales":
        return memory_service.resolve_knowledge_query(
            message,
            _knowledge_memory_view(working_memory),
        )
    if intent == "order_status" and extract_order_id(message) is None:
        order_id = _resolve_order_id(
            message,
            None,
            working_memory,
            memory_service,
        )
        if order_id is not None:
            return f"{message}，订单号 {order_id}"
        return f"{message}，订单查询"
    return message


def _split_request_subqueries(rewritten_query: str) -> list[str]:
    segments = re.split(
        r"(?:，|,)?(?:另外|顺便|同时还|再帮我|并且帮我|also\s+help\s+me)",
        rewritten_query,
        flags=re.IGNORECASE,
    )
    cleaned = [" ".join(segment.strip(" ，,；;。").split()) for segment in segments]
    return [segment for segment in cleaned if segment] or [rewritten_query.strip()]


def _deterministic_pre_route_plan(message: str) -> RequestRoutePlan | None:
    """Legacy context-free helper; the production graph uses chat_input directly."""
    rule = match_input_rule(message)
    if rule is None:
        return None
    query = rule.recorded_message or normalized_text(message) or "[空白输入]"
    return RequestRoutePlan(
        rewritten_query=query,
        subqueries=[RoutedSubquery(
            id="goal_1", query=query,
            disposition=rule.disposition, reason_code=rule.id,
        )],
    )


def _fallback_route_disposition(
    query: str,
    working_memory: dict[str, Any],
) -> str:
    hard_boundary = BOUNDARY_POLICY.route_guard(query)
    if hard_boundary is not None:
        return hard_boundary[0]
    if _is_identity_or_capability_question(query) or _is_safe_direct_request(query):
        return "direct_response"
    if classify_intent(query) == "purchase_guidance":
        return "direct_response"
    compact = re.sub(r"\s+", "", query.casefold())
    if compact in {"这个呢", "这个怎么样", "帮我查一下", "看看这个", "那这个呢"}:
        has_context = any(
            bool(working_memory.get(key)) for key in ("catalog", "order", "policy")
        )
        if not has_context:
            return "clarification"
    return "tool_planning"


def _enforce_route_boundaries(
    plan: RequestRoutePlan,
    *,
    original_message: str = "",
) -> RequestRoutePlan:
    """Legacy compatibility only; never apply these keyword overrides after LLM1."""
    enforced: list[RoutedSubquery] = []
    for item in plan.subqueries:
        hard_boundary = _hard_route_boundary(item.query)
        if hard_boundary:
            disposition = hard_boundary[0]
            reason_code = hard_boundary[1]
        else:
            disposition = item.disposition
            reason_code = item.reason_code
        enforced.append(
            item.model_copy(
                update={
                    "query": " ".join(item.query.split()),
                    "disposition": disposition,
                    "reason_code": reason_code,
                    "tasks": item.tasks if disposition == "tool_planning" else [],
                }
            )
        )

    raw_segments = _split_boundary_guard_segments(original_message)
    raw_hard_boundaries = [
        (segment, hard_boundary)
        for segment in raw_segments
        if (hard_boundary := _hard_route_boundary(segment)) is not None
    ]
    if raw_hard_boundaries:
        has_safe_raw_segment = any(
            _hard_route_boundary(segment) is None for segment in raw_segments
        )
        if not has_safe_raw_segment:
            raw_dispositions = {hard_boundary[0] for _, hard_boundary in raw_hard_boundaries}
            if any(item.disposition not in raw_dispositions for item in enforced):
                segment, (disposition, reason_code) = raw_hard_boundaries[0]
                enforced = [
                    RoutedSubquery(
                        id="sq_1",
                        query=segment,
                        disposition=disposition,
                        reason_code=f"{reason_code}_original_request",
                    )
                ]
        else:
            existing_dispositions = {item.disposition for item in enforced}
            next_index = max((int(item.id.rsplit("_", 1)[-1]) for item in enforced), default=0)
            goal_prefix = "goal" if any(item.id.startswith("goal_") for item in enforced) else "sq"
            for segment, (disposition, reason_code) in raw_hard_boundaries:
                if disposition in existing_dispositions:
                    continue
                next_index += 1
                enforced.append(
                    RoutedSubquery(
                        id=f"{goal_prefix}_{next_index}",
                        query=segment,
                        disposition=disposition,
                        reason_code=f"{reason_code}_original_request",
                    )
                )
                existing_dispositions.add(disposition)
    while True:
        admitted_ids = {
            task.id
            for item in enforced
            if item.disposition == "tool_planning"
            for task in item.tasks
        }
        changed = False
        normalized: list[RoutedSubquery] = []
        for item in enforced:
            missing_dependency = any(
                not set(task.depends_on) <= admitted_ids for task in item.tasks
            )
            if item.disposition != "tool_planning" or not missing_dependency:
                normalized.append(item)
                continue
            changed = True
            normalized.append(
                item.model_copy(
                    update={
                        "disposition": "clarification",
                        "reason_code": "dependency_not_admitted",
                        "clarification_question": "请补充需要用于后续查询或比较的具体对象。",
                        "tasks": [],
                    }
                )
            )
        enforced = normalized
        if not changed:
            break
    return RequestRoutePlan(
        rewritten_query=" ".join(plan.rewritten_query.split()),
        subqueries=enforced,
    )


def _hard_route_boundary(query: str) -> tuple[str, str] | None:
    return BOUNDARY_POLICY.route_guard(query)


def _split_boundary_guard_segments(message: str) -> list[str]:
    normalized = " ".join(message.split())
    if not normalized:
        return []
    segments = re.split(
        r"[；;。！？!?]+|(?:，|,)(?=\s*(?:另外|顺便|同时|再|并|然后|告诉|帮我|"
        r"写|查|查询|推荐|取消|修改|申请|把))|(?:另外|顺便|同时还|再帮我|并且帮我)|"
        r"并(?=(?:告诉|帮我|写|查|查询|推荐|取消|修改|申请|把))",
        normalized,
        flags=re.IGNORECASE,
    )
    cleaned = [segment.strip(" ，,；;。") for segment in segments]
    return [segment for segment in cleaned if segment] or [normalized]


def _boundary_from_route_plan(plan: RequestRoutePlan) -> dict[str, Any]:
    tool_subqueries = tool_planning_subqueries(plan)
    blocked = blocked_subqueries(plan)
    if tool_subqueries or any(g.disposition == "tool_planning" for g in plan.subqueries):
        reason = (
            "请求包含可自动处理的只读子任务；其他子任务将按各自边界单独说明"
            if blocked
            else "所有子任务均已通过只读能力准入"
        )
        return BOUNDARY_POLICY.for_classification("in_scope_auto", reason=reason).model_dump(
            mode="json"
        )
    dispositions = {item.disposition for item in plan.subqueries}
    if "security_refusal" in dispositions:
        classification = "security_refusal"
    elif "human_handoff" in dispositions:
        classification = "human_handoff_required"
    elif "out_of_scope" in dispositions:
        classification = "out_of_scope"
    else:
        classification = "in_scope_auto"
    return BOUNDARY_POLICY.for_classification(classification).model_dump(mode="json")


def _intent_from_route_plan(plan: RequestRoutePlan) -> str:
    tool_subqueries = tool_planning_subqueries(plan)
    if tool_subqueries or any(g.disposition == "tool_planning" for g in plan.subqueries):
        return "request_router"
    dispositions = list(dict.fromkeys(item.disposition for item in plan.subqueries))
    return " + ".join(dispositions) or "clarification"


def _routed_query_for_call(state: AgentState, call: PlannedToolCall) -> str | None:
    route_plan_value = state.get("route_plan")
    tasks = tool_planning_subqueries(route_plan_value)
    if call.name == "order_lookup" and len(tasks) == 1:
        try:
            route_plan = RequestRoutePlan.model_validate(route_plan_value)
        except (TypeError, ValueError):
            route_plan = None
        if route_plan is not None and len(route_plan.subqueries) == 1:
            # Order capability classification depends on preserving every requested field.
            # A Router canonicalization such as "查询所有订单" must not erase "多少",
            # "商品明细", or an unusual unsupported requirement before the Order planner.
            original_message = str(state.get("message") or "").strip()
            if original_message:
                return original_message
    for item in tasks:
        if item.id == call.subquery.strip():
            return item.query
    return None
