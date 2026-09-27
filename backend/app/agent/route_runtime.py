"""Request Router normalization, deterministic guards, and route projections."""

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.boundary import BOUNDARY_POLICY
from app.agent.decisions import PlannedToolCall
from app.agent.intent import classify_intent, extract_order_id
from app.agent.prompts import (
    REQUEST_ROUTER_SYSTEM_PROMPT,
    build_request_router_user_prompt,
)
from app.agent.responses import (
    _is_identity_or_capability_question,
    _is_safe_direct_request,
)
from app.agent.routing import (
    RequestRoutePlan,
    RoutedSubquery,
    RoutedTask,
    blocked_subqueries,
    tool_planning_subqueries,
)
from app.agent.state import AgentState
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


def _reuse_comparison_context(
    plan: RequestRoutePlan,
    working_memory: dict[str, Any],
) -> RequestRoutePlan:
    """Collapse redundant discovery tasks when Router targets an existing comparison pair."""
    tasks = {item.id: item for item in tool_planning_subqueries(plan)}
    if len(tasks) == 1:
        only_task = next(iter(tasks.values()))
        sources = {item.source for item in only_task.input_requirements}
        if only_task.capability == "catalog_compare" and sources == {"comparison_context"}:
            return plan

    for comparison_task in tasks.values():
        if comparison_task.capability != "catalog_compare" or not comparison_task.depends_on:
            continue
        comparison_level = comparison_task.comparison_level
        if comparison_level is None:
            spu_ids = _context_comparison_ids(working_memory, "spu")
            sku_ids = _context_comparison_ids(working_memory, "sku")
            comparison_ids = spu_ids if len(spu_ids) >= 2 else sku_ids
        else:
            comparison_ids = _context_comparison_ids(
                working_memory,
                comparison_level,
            )
        if len(comparison_ids) < 2:
            continue
        dependency_ids = set(comparison_task.depends_on)
        if set(tasks) != dependency_ids | {comparison_task.id}:
            continue
        dependencies = [tasks[item_id] for item_id in comparison_task.depends_on]
        if not all(
            item.capability == "catalog_search" and item.result_selector is None
            for item in dependencies
        ):
            continue
        matched_ids = {
            comparison_id
            for item in dependencies
            for comparison_id in comparison_ids
            if re.search(rf"(?<!\d){comparison_id}(?!\d)", item.query)
        }
        if matched_ids != set(comparison_ids):
            continue
        collapsed = RoutedTask.model_validate(
            {
                **comparison_task.model_dump(mode="python"),
                "depends_on": [],
                "input_requirements": [
                    {
                        "name": "comparison_products",
                        "source": "comparison_context",
                    }
                ],
            }
        )
        goal = next(
            item for item in plan.subqueries if item.id == comparison_task.goal_id
        )
        return RequestRoutePlan(
            rewritten_query=plan.rewritten_query,
            subqueries=[
                goal.model_copy(
                    update={
                        "reason_code": "reuse_confirmed_comparison_context",
                        "tasks": [collapsed],
                    }
                )
            ],
        )
    return plan


def _ground_candidate_comparison(
    plan: RequestRoutePlan,
    working_memory: dict[str, Any],
    message: str,
) -> RequestRoutePlan:
    """Inherit the prior result entity scope for ordinal candidate comparisons."""
    catalog = working_memory.get("catalog")
    query_plan = catalog.get("query_plan") if isinstance(catalog, dict) else None
    selection_scope = (
        query_plan.get("selection_scope") if isinstance(query_plan, dict) else None
    )
    if selection_scope not in {"sku", "spu"}:
        return plan
    has_structured_catalog_candidates = any(
        task.capability == "catalog_compare"
        and any(
            requirement.source == "catalog_candidates"
            for requirement in task.input_requirements
        )
        for subquery in plan.subqueries
        for task in subquery.tasks
    )
    has_fallback_candidates = (
        len(_resolve_compare_sku_ids(message, working_memory)) >= 2
    )
    if not has_structured_catalog_candidates and not has_fallback_candidates:
        return plan
    is_comparison = any(
        marker in message for marker in ("对比", "比较", "区别", "哪个好")
    )
    if not is_comparison:
        return plan

    changed = False
    subqueries: list[RoutedSubquery] = []
    for subquery in plan.subqueries:
        if (
            len(plan.subqueries) == 1
            and subquery.disposition == "clarification"
            and not subquery.tasks
        ):
            comparison_task = RoutedTask.model_validate(
                {
                    "id": "task_1",
                    "goal_id": subquery.id,
                    "canonical_query": subquery.query,
                    "depends_on": [],
                    "input_requirements": [],
                    "produces": "comparison",
                    "answer_role": "user_facing",
                    "capability": "catalog_compare",
                    "comparison_level": selection_scope,
                }
            )
            subqueries.append(
                subquery.model_copy(
                    update={
                        "disposition": "tool_planning",
                        "reason_code": "grounded_candidate_comparison",
                        "missing_information": [],
                        "clarification_question": "",
                        "tasks": [comparison_task],
                    }
                )
            )
            changed = True
            continue
        tasks: list[RoutedTask] = []
        for task in subquery.tasks:
            if (
                task.capability == "catalog_compare"
                and task.comparison_level is None
                and not task.depends_on
                and (
                    not task.input_requirements
                    or {
                        requirement.source
                        for requirement in task.input_requirements
                    }
                    == {"catalog_candidates"}
                )
            ):
                task = task.model_copy(update={"comparison_level": selection_scope})
                changed = True
            tasks.append(task)
        subqueries.append(
            subquery.model_copy(update={"tasks": tasks}) if tasks != subquery.tasks else subquery
        )
    if not changed:
        return plan
    return RequestRoutePlan(
        rewritten_query=plan.rewritten_query,
        subqueries=subqueries,
    )


def _resolve_compare_sku_ids(message: str, working_memory: dict[str, Any]) -> list[int]:
    catalog = working_memory.get("catalog")
    if not isinstance(catalog, dict):
        return []
    candidates = catalog.get("candidate_sku_ids")
    if not isinstance(candidates, list):
        return []

    indexes: list[int] = []
    ordinal_markers = [
        (0, ("第一个", "第一款", "第1个", "第1款", "1号")),
        (1, ("第二个", "第二款", "第2个", "第2款", "2号")),
        (2, ("第三个", "第三款", "第3个", "第3款", "3号")),
        (3, ("第四个", "第四款", "第4个", "第4款", "4号")),
        (4, ("第五个", "第五款", "第5个", "第5款", "5号")),
        (5, ("第六个", "第六款", "第6个", "第6款", "6号")),
    ]
    for index, markers in ordinal_markers:
        if any(marker in message for marker in markers):
            indexes.append(index)
    is_compare = any(term in message for term in ("对比", "比较", "区别", "哪个好"))
    collective_count = _collective_reference_count(message)
    if (
        not indexes
        and is_compare
        and collective_count is not None
        and collective_count == len(candidates)
    ):
        indexes = list(range(collective_count))
    if not indexes and is_compare and any(
        term in message for term in ("这些", "这几个", "上面的", "它们")
    ):
        indexes = list(range(len(candidates)))
    if (
        not indexes
        and is_compare
        and len(candidates) == 2
        and any(term in message for term in ("两者", "二者"))
    ):
        indexes = [0, 1]
    if not indexes and any(term in message for term in ("这款", "这个", "刚才那个")):
        referenced_sku_id = catalog.get("referenced_sku_id")
        if isinstance(referenced_sku_id, int) and referenced_sku_id in candidates:
            indexes = [candidates.index(referenced_sku_id)]
        elif candidates:
            indexes = [0]

    resolved: list[int] = []
    for index in indexes:
        if index >= len(candidates):
            continue
        value = candidates[index]
        if isinstance(value, int) and value not in resolved:
            resolved.append(value)
    return resolved[:10]


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
        or order.get("next_offset") is not None
    )
    return has_order_context and (
        _order_ordinal_index(message) is not None
        or any(
            marker in message
            for marker in (
                "下一页",
                "继续列",
                "继续看",
                "剩下",
                "剩余订单",
                "其他订单",
                "还有订单",
                "还有别的",
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
    dependency_feedback = _catalog_dependency_materialization_feedback(
        plan,
        has_catalog_reference=has_catalog_reference,
    )
    if dependency_feedback is not None:
        return dependency_feedback
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


def _catalog_dependency_materialization_feedback(
    plan: RequestRoutePlan,
    *,
    has_catalog_reference: bool,
) -> dict[str, Any] | None:
    if not has_catalog_reference:
        return None
    tasks = {task.id: task for task in tool_planning_subqueries(plan)}
    for task in tasks.values():
        if not task.depends_on or not any(
            marker in task.query for marker in ("对比", "比较", "区别", "哪个好")
        ):
            continue
        if not _has_catalog_context_reference_language(task.query):
            continue
        dependencies = [tasks.get(task_id) for task_id in task.depends_on]
        if any(
            dependency is not None
            and _has_catalog_context_reference_language(dependency.query)
            for dependency in dependencies
        ):
            continue
        return {
            "code": "dependency_must_materialize_catalog_context",
            "instruction": (
                "A dependent comparison Task still reads the current/previous product directly. "
                "Only root Tasks may read working memory. Rewrite the root discovery Task so it "
                "returns both the working-memory product and every newly retrieved ranked product. "
                "Rewrite the dependent Task to compare only the products returned by its direct "
                "dependency; keep input_requirements empty and do not bind IDs."
            ),
            "dependent_task_id": task.id,
            "dependency_task_ids": list(task.depends_on),
        }
    return None


def _has_catalog_context_reference_language(query: str) -> bool:
    compact = re.sub(r"\s+", "", query.casefold())
    return any(
        marker in compact
        for marker in (
            "当前商品",
            "当前系列",
            "当前键盘",
            "当前鼠标",
            "当前耳机",
            "当前显示器",
            "这个商品",
            "这款商品",
            "这个系列",
            "这个键盘",
            "这个鼠标",
            "这个耳机",
            "这个显示器",
            "这款",
        )
    )


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
    """Return a terminal route plan only when every raw segment is unambiguous."""
    normalized = " ".join(message.split())
    if not normalized:
        return None

    subqueries: list[RoutedSubquery] = []
    for segment in _split_boundary_guard_segments(normalized):
        hard_boundary = _hard_route_boundary(segment)
        if hard_boundary is not None:
            disposition, reason_code = hard_boundary
        elif _is_high_confidence_direct_request(segment):
            disposition = "direct_response"
            reason_code = "runtime_direct_fast_path"
        elif subqueries and _is_terminal_boundary_continuation(segment):
            previous = subqueries[-1]
            if previous.disposition == "direct_response":
                return None
            subqueries[-1] = previous.model_copy(
                update={"query": f"{previous.query}，{segment}"}
            )
            continue
        else:
            # One executable or ambiguous segment means the Router must still rewrite and split
            # the complete request. This preserves mixed-intent and working-memory behavior.
            return None
        subqueries.append(
            RoutedSubquery(
                id=f"sq_{len(subqueries) + 1}",
                query=segment,
                disposition=disposition,
                reason_code=reason_code,
            )
        )
    return RequestRoutePlan(rewritten_query=normalized, subqueries=subqueries)


def _is_terminal_boundary_continuation(message: str) -> bool:
    compact = re.sub(r"\s+", "", message.casefold())
    if any(
        marker in compact
        for marker in (
            "推荐",
            "比较",
            "对比",
            "查询订单",
            "查订单",
            "物流",
            "退货政策",
            "发票政策",
        )
    ):
        return False
    return any(
        marker in compact
        for marker in ("对应", "这个", "这款", "它", "结果", "sku", "型号", "编号")
    )


def _is_high_confidence_direct_request(message: str) -> bool:
    if _is_identity_or_capability_question(message):
        return True
    compact = re.sub(r"[\s，。！？!?、,.]", "", message.casefold())
    if compact in {
        "你好",
        "您好",
        "hello",
        "hi",
        "谢谢",
        "谢谢你",
        "再见",
    }:
        return True
    return classify_intent(message) == "purchase_guidance"


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
            raw_dispositions = {
                hard_boundary[0] for _, hard_boundary in raw_hard_boundaries
            }
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
    elif not _route_has_order_lookup_task(enforced):
        # Router owns security/write boundaries, but it must not guess whether an arbitrary
        # read-only field exists in Order Tool output. Validate only the raw order segment;
        # never replace unrelated Catalog or Policy goals in a mixed request.
        order_segments = [
            segment
            for segment in raw_segments
            if _is_read_only_order_fact_request(segment)
        ]
        if order_segments:
            if len(raw_segments) == 1:
                enforced = [
                    _order_capability_validation_subquery(
                        goal_id="goal_1",
                        task_id="task_1",
                        query=original_message,
                    )
                ]
            else:
                next_goal_index = max(
                    (int(item.id.rsplit("_", 1)[-1]) for item in enforced),
                    default=0,
                )
                next_task_index = max(
                    (
                        int(task.id.rsplit("_", 1)[-1])
                        for item in enforced
                        for task in item.tasks
                    ),
                    default=0,
                )
                goal_prefix = (
                    "goal"
                    if any(item.id.startswith("goal_") for item in enforced)
                    else "sq"
                )
                for segment in order_segments:
                    next_goal_index += 1
                    next_task_index += 1
                    goal_id = f"{goal_prefix}_{next_goal_index}"
                    enforced.append(
                        _order_capability_validation_subquery(
                            goal_id=goal_id,
                            task_id=f"task_{next_task_index}",
                            query=segment,
                        )
                    )
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


def _session_grounded_route_allowed(
    plan: RequestRoutePlan,
    history: list[dict[str, Any]],
    *,
    original_message: str = "",
) -> bool:
    """Apply a small deterministic veto to the Router's high-confidence reuse decision."""
    if not any(
        item.disposition == "session_grounded_response" for item in plan.subqueries
    ):
        return True
    has_assistant_history = any(
        item.get("role") == "assistant" and str(item.get("content") or "").strip()
        for item in history
    )
    if not has_assistant_history:
        return False
    text_to_check = "；".join(
        [original_message, plan.rewritten_query, *(item.query for item in plan.subqueries)]
    )
    compact = re.sub(r"\s+", "", text_to_check.casefold())
    refresh_markers = (
        "现在",
        "当前",
        "最新",
        "实时",
        "有没有变化",
        "有变化吗",
        "变了吗",
        "还有货",
        "库存",
        "物流到哪",
        "到哪了",
    )
    return not any(marker in compact for marker in refresh_markers)


def _hard_route_boundary(query: str) -> tuple[str, str] | None:
    return BOUNDARY_POLICY.route_guard(query)


def _is_read_only_order_fact_request(message: str) -> bool:
    compact = re.sub(r"\s+", "", message.casefold())
    if not compact or not any(marker in compact for marker in ("订单", "单子", "下单")):
        return False
    if any(
        marker in compact
        for marker in (
            "取消",
            "修改",
            "改地址",
            "催单",
            "催发货",
            "退款",
            "退货",
            "换货",
            "维修",
            "保修",
            "价保",
            "售后",
            "申请",
            "导出",
            "下载",
            "政策",
            "规则",
            "流程",
            "怎么办",
        )
    ):
        return False
    return any(
        marker in compact
        for marker in (
            "查",
            "找",
            "翻",
            "看看",
            "瞅",
            "列",
            "哪些",
            "哪个",
            "什么",
            "哪里",
            "哪儿",
            "多少",
            "有没有",
            "能不能",
            "能把",
            "每一",
            "每笔",
            "每张",
            "分别",
        )
    )


def _route_has_order_lookup_task(subqueries: list[RoutedSubquery]) -> bool:
    return any(
        task.capability == "order_lookup"
        or (
            task.capability == "planner_required"
            and _is_read_only_order_fact_request(task.canonical_query)
        )
        for subquery in subqueries
        for task in subquery.tasks
    )


def _order_capability_validation_subquery(
    *,
    goal_id: str,
    task_id: str,
    query: str,
) -> RoutedSubquery:
    normalized_query = " ".join(query.split())
    return RoutedSubquery(
        id=goal_id,
        query=normalized_query,
        disposition="tool_planning",
        reason_code="runtime_order_capability_validation",
        tasks=[
            RoutedTask(
                id=task_id,
                goal_id=goal_id,
                canonical_query=normalized_query,
                depends_on=[],
                input_requirements=[],
                produces="order",
                answer_role="user_facing",
                capability="order_lookup",
            )
        ],
    )


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
    if tool_subqueries:
        reason = (
            "请求包含可自动处理的只读子任务；其他子任务将按各自边界单独说明"
            if blocked
            else "所有子任务均已通过只读能力准入"
        )
        return BOUNDARY_POLICY.for_classification(
            "in_scope_auto", reason=reason
        ).model_dump(mode="json")
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
    if tool_subqueries:
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


def _context_comparison_sku_ids(working_memory: dict[str, Any]) -> list[int]:
    return _context_comparison_ids(working_memory, "sku")


def _context_comparison_ids(
    working_memory: dict[str, Any],
    comparison_level: str,
) -> list[int]:
    catalog = working_memory.get("catalog")
    if not isinstance(catalog, dict):
        return []
    comparison = catalog.get("comparison")
    key = "spu_ids" if comparison_level == "spu" else "sku_ids"
    values = comparison.get(key) if isinstance(comparison, dict) else None
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            item
            for item in values
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        )
    )[:10]
