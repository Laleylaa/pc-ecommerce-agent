"""Fallback answers, terminal templates, and response suggestions."""

import re
from typing import Any

from app.agent.boundary import BOUNDARY_POLICY
from app.agent.intent import classify_intent, extract_order_id
from app.agent.outcomes import (
    active_usable_tool_call_ids,
    is_active_ledger_entry,
    normalize_tool_result,
)
from app.agent.routing import RequestRoutePlan, RoutedSubquery, blocked_subqueries
from app.agent.state import AgentState

CUSTOMER_SPEC_LABELS = {
    "backlit": "背光",
    "channels": "声道",
    "color": "颜色",
    "connection_type": "连接方式",
    "enclosure_type": "耳罩类型",
    "field_of_view": "视野范围",
    "frame_rate": "帧率",
    "frequency_response": "频响范围",
    "hand_orientation": "持握方向",
    "max_dpi": "最高 DPI",
    "microphone": "麦克风",
    "panel_type": "面板类型",
    "power_w": "功率",
    "refresh_rate": "刷新率",
    "resolution": "分辨率",
    "response_time_ms": "响应时间",
    "size_inch": "尺寸",
    "style": "款式",
    "switches": "轴体",
    "tenkeyless": "键盘布局",
    "tracking_method": "传感方式",
    "type": "类型",
    "weight_g": "重量",
    "wireless": "无线连接",
}


def _safe_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _fallback_answer(state: AgentState) -> str:
    products = state.get("products", [])
    if products:
        product_search = state.get("parsed", {}).get("product_search", {})
        product_search = product_search if isinstance(product_search, dict) else {}
        usage_mapping = (
            product_search.get("usage_mapping", {})
        )
        usage_status = (
            str(usage_mapping.get("status") or "") if isinstance(usage_mapping, dict) else ""
        )
        result_purpose = str(product_search.get("result_purpose") or "search")
        selection_scope = str(product_search.get("selection_scope") or "spu")
        total_match_count = _safe_non_negative_int(
            product_search.get("total_match_count")
        )
        returned_count = _safe_non_negative_int(product_search.get("returned_count"))
        is_exhaustive = bool(product_search.get("is_exhaustive", True))
        scope_label = "具体版本" if selection_scope == "sku" else "商品系列"
        if result_purpose == "recommendation":
            intro = "我按当前目录的综合推荐顺序整理了这些候选，第一项是首选："
        elif result_purpose == "lookup":
            intro = "我查到了这个商品的以下目录信息："
        elif result_purpose == "ranking":
            intro = "我按用户指定的目录指标查到了以下排名结果："
        elif usage_status == "applied":
            intro = "我根据这个使用场景相关的规格要求和偏好，找到了这些候选："
        elif usage_status == "expanded":
            intro = "我根据这个使用场景，从多个相关外设品类中找到了这些候选："
        else:
            intro = "我根据商品目录找到了这些候选："
        lines = [intro]
        if (
            result_purpose != "ranking"
            and not is_exhaustive
            and total_match_count > 0
        ):
            lines.append(
                f"共匹配 {total_match_count} 个{scope_label}，"
                f"本次返回 {returned_count or len(products)} 个候选。"
            )
        asks_sales = any(
            term in state.get("message", "").lower() for term in ("销量", "热销", "畅销", "sales")
        )
        for index, product in enumerate(products[:3]):
            is_series = product.entity_scope == "spu" or product.ranking_scope == "spu"
            source_specs = (
                product.series_common_specs if is_series else product.specs
            )
            customer_specs = [
                (CUSTOMER_SPEC_LABELS[key.casefold()], value)
                for key, value in source_specs.items()
                if key.casefold() in CUSTOMER_SPEC_LABELS
            ][:4]
            if is_series:
                for key, values in product.series_option_specs.items():
                    label = CUSTOMER_SPEC_LABELS.get(key.casefold())
                    if label and values and len(customer_specs) < 4:
                        customer_specs.append((f"可选{label}", "/".join(values)))
            specs = "，".join(f"{label}: {value}" for label, value in customer_specs)
            suffix = f"，{specs}" if specs else ""
            if asks_sales:
                suffix += (
                    f"，商品系列累计销量 {product.sales_count}"
                    if is_series
                    else (
                        f"，当前版本销量 {product.sku_sales_count}，"
                        f"整个商品系列累计销量 {product.sales_count}"
                    )
                )
            if is_series:
                title = product.spu_title or product.title
                minimum = product.series_min_price or product.price
                maximum = product.series_max_price or minimum
                price = f"¥{minimum}" if minimum == maximum else f"¥{minimum}–¥{maximum}"
                stock = product.series_total_stock
                version_count = product.series_sku_count
                role = (
                    "首选 · "
                    if result_purpose == "recommendation" and index == 0
                    else "备选 · "
                    if result_purpose == "recommendation"
                    else ""
                )
                lines.append(
                    f"- {role}{title}：{price}，系列库存 {stock or 0}，"
                    f"共 {version_count or 1} 个在售版本{suffix}。"
                )
            else:
                role = (
                    "首选 · "
                    if result_purpose == "recommendation" and index == 0
                    else "备选 · "
                    if result_purpose == "recommendation"
                    else ""
                )
                lines.append(
                    f"- {role}{product.title}：¥{product.price}，库存 "
                    f"{product.stock}{suffix}。"
                )
        if usage_status in {"applied", "expanded"}:
            lines.append(
                "如果你想继续缩小范围，可以补充预算或最在意的具体规格；实际价格和库存以下单页为准。"
            )
        else:
            lines.append(
                "告诉我主要用途后，我可以继续判断哪一款更适合；实际价格和库存以下单页为准。"
            )
        return "\n".join(lines)

    order = state.get("order")
    if order:
        logistics = order.logistics
        shipping = "暂未查询到物流单号"
        if logistics and logistics.logistic_no:
            shipping = f"{logistics.express_company or '快递'} {logistics.logistic_no}"
        return (
            f"订单 {order.id} 当前状态为「{order.status_label}」，实付 ¥{order.pay_amount}。\n"
            f"物流：{shipping}。"
        )

    candidates = state.get("parsed", {}).get("order_candidates", [])
    order_query = state.get("parsed", {}).get("order_query", {})
    if isinstance(order_query, dict) and order_query.get("query_mode") == "count":
        total = int(order_query.get("total_match_count") or 0)
        return f"你当前一共有 {total} 个订单。"
    if (
        isinstance(order_query, dict)
        and order_query.get("query_mode") in {"page", "remaining"}
        and int(order_query.get("returned_count") or 0) == 0
        and int(order_query.get("total_match_count") or 0) > 0
    ):
        total = int(order_query["total_match_count"])
        return f"已经列完全部 {total} 个订单，没有其他订单了。"
    if candidates:
        total = int(order_query.get("total_match_count") or len(candidates))
        returned = int(order_query.get("returned_count") or len(candidates))
        offset = int(order_query.get("offset") or 0)
        is_exhaustive = bool(order_query.get("is_exhaustive", True))
        if offset:
            lines = [f"你一共有 {total} 个订单，下面是第 {offset + 1} 条起的 {returned} 个："]
        else:
            lines = [f"你一共有 {total} 个订单，下面列出最近的 {returned} 个："]
        lines.extend(
            f"- 订单 {item['id']}：{item['status_label']}，实付 ¥{item['pay_amount']}。"
            for item in candidates
        )
        if not is_exhaustive:
            lines.append("这只是部分结果；如果需要，我可以继续列出剩余订单。")
        lines.append("告诉我订单号或“第几个”，我可以继续查看该订单明细。")
        return "\n".join(lines)

    evidence = state.get("evidence", [])
    if evidence:
        if len(evidence) > 1:
            titles = "、".join(item.title for item in evidence[:3])
            return (
                f"我找到了可能相关的资料（{titles}），但暂时无法可靠归纳出直接答案。"
                "请稍后重试，或把问题描述得更具体一些。"
            )
        lines = ["我根据知识库查到以下信息："]
        lines.extend(f"- {item.title}：{item.snippet}" for item in evidence)
        lines.append("依据：" + "、".join(item.title for item in evidence))
        return "\n".join(lines)

    facet_output = _latest_successful_tool_output(state, "catalog_facets")
    if facet_output is not None:
        return _catalog_facets_fallback_answer(facet_output)

    failed_results = [
        result
        for result in _active_tool_results(state)
        if not result.get("execution", {}).get("ok")
    ]
    if failed_results:
        return "业务信息查询暂时失败，请稍后重试或补充更具体的信息。"
    return "我暂时没有找到足够的信息，请补充具体商品、订单号或想咨询的政策。"


def _fallback_catalog_facets_arguments(message: str) -> dict[str, Any] | None:
    compact = message.lower().replace(" ", "")
    list_markers = ("哪些", "有什么", "有啥", "都有什么", "都有哪些", "可选", "提供")
    asks_for_list = any(marker in compact for marker in list_markers)
    if not asks_for_list:
        return None

    if any(term in compact for term in ("品牌", "牌子")):
        return {"query": message, "limit": 20}
    if any(term in compact for term in ("品类", "类目", "商品类型", "外设类型")):
        return {"query": message, "limit": 20}
    if any(term in compact for term in ("轴体", "刷新率", "分辨率", "连接方式", "dpi", "颜色")):
        return {"query": message, "limit": 20}
    return None


def _latest_successful_tool_output(
    state: AgentState,
    tool_name: str,
) -> dict[str, Any] | None:
    for result in reversed(_active_tool_results(state)):
        execution = result.get("execution", {})
        if result.get("name") == tool_name and execution.get("ok"):
            output = execution.get("output")
            if isinstance(output, dict):
                return output
    return None


def _active_tool_results(state: AgentState) -> list[dict[str, Any]]:
    ledger = state.get("subquery_ledger", [])
    if not ledger:
        return state.get("tool_results", [])
    active_ids = {
        str(entry.get("tool_call_id"))
        for entry in ledger
        if is_active_ledger_entry(entry) and entry.get("tool_call_id")
    }
    return [
        result
        for result in state.get("tool_results", [])
        if str(result.get("tool_call_id") or "") in active_ids
    ]


def _has_successful_tool_result(state: AgentState) -> bool:
    return bool(_usable_tool_call_ids(state))


def _usable_tool_call_ids(state: AgentState) -> list[str]:
    artifacts = state.get("task_artifacts")
    if isinstance(artifacts, dict):
        return list(
            dict.fromkeys(
                str(artifact.get("source_tool_call_id"))
                for artifact in artifacts.values()
                if isinstance(artifact, dict)
                and artifact.get("usable")
                and artifact.get("source_tool_call_id")
            )
        )
    ledger = state.get("subquery_ledger", [])
    if ledger:
        return active_usable_tool_call_ids(ledger)
    return [
        outcome.tool_call_id
        for outcome in (normalize_tool_result(result) for result in state.get("tool_results", []))
        if outcome.has_usable_information and outcome.tool_call_id
    ]


def _fallback_unavailable_answer(state: AgentState) -> str:
    outcomes = [normalize_tool_result(result) for result in _active_tool_results(state)]
    kinds = {outcome.outcome for outcome in outcomes}
    tool_names = {outcome.tool_name for outcome in outcomes}
    catalog_output = _latest_successful_tool_output(state, "catalog_search")
    if catalog_output and _has_catalog_diagnostic(catalog_output, "usage_mapping_unavailable"):
        return (
            "当前目录没有找到能够依据现有规格确认适合这个使用场景的商品。"
            "你可以补充预算、连接方式、重量或最在意的具体规格，我再继续筛选。"
        )
    if "unsupported" in kinds:
        return (
            "这个请求属于 PC 外设相关，但当前商品查询能力不支持这类信息。"
            "你可以改问具体商品、价格、库存、规格或目录筛选。"
        )
    if "error" in kinds:
        return "业务信息查询暂时失败，请稍后重试。"
    if "not_found" in kinds and tool_names == {"order_lookup"}:
        return "当前账号下没有找到对应订单。请核对订单号，或让我查询最近订单。"
    if "insufficient" in kinds and "catalog_compare" in tool_names:
        return "当前只找到不足两款可比商品，暂时无法完成有效对比。请补充具体型号。"
    if tool_names <= {"policy_search", "knowledge_search"}:
        return "当前知识库没有找到足够依据，我不能凭模型常识补写答案。"
    if tool_names <= {"catalog_search", "catalog_compare", "catalog_facets"}:
        return "当前商品目录没有找到匹配信息。你可以补充型号，或放宽一个筛选条件。"
    return "本次查询没有得到可用于回答的信息，请补充更具体的商品、订单或政策问题。"


LATE_HANDOFF_CONFIRMATION = (
    "这个需求当前智能客服无法满足，需要人工客服进一步处理。是否需要转人工客服？"
)
MIXED_HANDOFF_CONFIRMATION = (
    "你的另一个需求当前智能客服无法满足，需要人工客服进一步处理。是否需要转人工客服？"
)


def _append_late_handoff_confirmation(response: str) -> str:
    """Render the only Answer-stage handoff behavior without changing frontend state."""
    return f"{response.rstrip()}\n\n{MIXED_HANDOFF_CONFIRMATION}"


def _has_catalog_diagnostic(output: dict[str, Any], code: str) -> bool:
    diagnostics = output.get("diagnostics")
    if not isinstance(diagnostics, list):
        return False
    return any(
        isinstance(diagnostic, dict) and diagnostic.get("code") == code
        for diagnostic in diagnostics
    )


def _catalog_facets_fallback_answer(output: dict[str, Any]) -> str:
    facet_labels = {
        "brand": "品牌",
        "category": "类目",
        "spec_key": "规格字段",
        "spec_value": "规格选项",
    }
    label = facet_labels.get(str(output.get("facet")), "目录选项")
    items = output.get("items")
    if not isinstance(items, list) or not items:
        return f"当前目录中没有找到符合条件的{label}。"

    rendered: list[str] = []
    for item in items[:10]:
        if not isinstance(item, dict) or not item.get("value"):
            continue
        count = item.get("count")
        suffix = f"（{count} 条 SKU 记录）" if isinstance(count, int) else ""
        rendered.append(f"{item['value']}{suffix}")
    if not rendered:
        return f"当前目录中没有找到符合条件的{label}。"
    return f"当前目录中可查到的{label}包括：" + "、".join(rendered) + "。"


def _purchase_guidance_answer() -> str:
    return (
        "下单流程可以按这几步走：\n"
        "1. 在商品页确认 SKU、价格、库存和关键规格。\n"
        "2. 选择规格并确认收货信息。\n"
        "3. 提交订单后按页面提示支付，再到订单页查看状态和物流。\n"
        "我不能在聊天中替你提交订单或完成支付。"
    )


def _store_philosophy_answer() -> str:
    return (
        "我们的服务理念是：围绕 PC 外设，用清晰、克制、有依据的信息帮助你做选择。"
        "商品建议尽量结合真实价格、库存、销量和规格；订单与政策问题尊重隐私和权限，"
        "需要实际执行或人工确认的事项不会替你擅自承诺。"
    )


def _is_store_philosophy_question(message: str) -> bool:
    compact = re.sub(r"\s+", "", message.casefold())
    return any(term in compact for term in ("理念", "使命", "价值观", "宗旨"))


def _is_identity_or_capability_question(message: str) -> bool:
    compact = re.sub(r"[\s，。！？!?、,.]", "", message.casefold())
    if any(
        term in compact for term in ["你是谁", "你是什么", "你能做什么", "你会什么", "怎么用你"]
    ):
        return True
    return bool(
        re.search(
            r"(?:你|这里|这个客服).{0,4}(?:是|是不是|属于).{0,6}"
            r"(?:人工|智能|真人)(?:客服|助手)",
            compact,
        )
        or re.search(
            r"(?:人工|智能|真人)(?:客服|助手).*(?:区别|不同|一样|哪个|还是)",
            compact,
        )
    )


def _is_safe_direct_request(message: str) -> bool:
    if _is_identity_or_capability_question(message):
        return True
    if classify_intent(message) == "purchase_guidance":
        return True
    compact = re.sub(r"[\s，。！？!?、,.]", "", message.lower())
    return compact in {
        "你好",
        "您好",
        "hello",
        "hi",
        "谢谢",
        "谢谢你",
        "再见",
    }


def _route_notice(subquery: RoutedSubquery) -> str:
    if subquery.disposition == "clarification":
        return subquery.clarification_question.strip()
    if subquery.disposition == "direct_response":
        if _is_store_philosophy_question(subquery.query):
            return _store_philosophy_answer()
        if classify_intent(subquery.query) == "purchase_guidance":
            return _purchase_guidance_answer()
        if _is_identity_or_capability_question(subquery.query):
            return (
                "我是 PC 外设商城客服 AI，可以帮你推荐和对比外设、查询当前账号的订单物流，"
                "以及说明商城政策和外设选购知识。"
            )
        if any(term in subquery.query.casefold() for term in ("谢谢", "thank")):
            return "不客气，有外设、订单物流或商城政策问题都可以继续问我。"
        if any(term in subquery.query.casefold() for term in ("再见", "bye")):
            return "再见，有外设、订单物流或商城政策问题时欢迎再来。"
        return "你好，我可以帮你处理 PC 外设推荐、本人订单物流和商城政策咨询。"
    classification_by_disposition = {
        "human_handoff": "human_handoff_required",
        "out_of_scope": "out_of_scope",
        "security_refusal": "security_refusal",
    }
    classification = classification_by_disposition.get(subquery.disposition)
    if classification is None:
        return ""
    return BOUNDARY_POLICY.for_classification(classification).display_message


def _route_terminal_answer(state: AgentState, disposition: str) -> str:
    route_plan = state.get("route_plan")
    if route_plan:
        plan = RequestRoutePlan.model_validate(route_plan)
        has_direct_answer = any(
            item.disposition == "direct_response" for item in plan.subqueries
        )
        notices = []
        for item in plan.subqueries:
            if item.disposition == "tool_planning":
                continue
            if has_direct_answer and item.disposition == "human_handoff":
                notices.append(MIXED_HANDOFF_CONFIRMATION)
            else:
                notices.append(_route_notice(item))
        unique = [notice for notice in dict.fromkeys(notices) if notice]
        if unique:
            return "\n\n".join(unique)
    classification_by_disposition = {
        "human_handoff": "human_handoff_required",
        "out_of_scope": "out_of_scope",
        "security_refusal": "security_refusal",
    }
    classification = classification_by_disposition.get(disposition)
    if classification is not None:
        return BOUNDARY_POLICY.for_classification(classification).display_message
    if disposition == "clarification":
        return "你具体想咨询哪款商品、哪笔订单或哪项商城服务？"
    if disposition == "session_grounded_response":
        for item in reversed(state.get("history", [])):
            if item.get("role") == "assistant" and str(item.get("content") or "").strip():
                return str(item["content"]).strip()
        return "我暂时无法确认刚才结果中的具体信息，需要重新查询后再回答。"
    return "你好，我可以帮你处理 PC 外设推荐、本人订单物流和商城政策咨询。"


def _append_blocked_route_notices(answer: str, state: AgentState) -> str:
    blocked = blocked_subqueries(state.get("route_plan"))
    if not blocked:
        return answer

    handoff_blocked = any(item.disposition == "human_handoff" for item in blocked)
    notices = [
        _route_notice(item)
        for item in blocked
        if item.disposition != "human_handoff"
    ]
    unique = [notice for notice in dict.fromkeys(notices) if notice and notice not in answer]
    suffixes: list[str] = []
    if handoff_blocked and not any(
        template in answer
        for template in (LATE_HANDOFF_CONFIRMATION, MIXED_HANDOFF_CONFIRMATION)
    ):
        decision = state.get("decision")
        control_action = (
            str(decision.get("control_action") or "")
            if isinstance(decision, dict)
            else ""
        )
        suffixes.append(
            MIXED_HANDOFF_CONFIRMATION
            if control_action in {"finish_answer", "finish_partial"}
            else LATE_HANDOFF_CONFIRMATION
        )
    if unique:
        suffixes.append("另外：\n" + "\n".join(f"- {notice}" for notice in unique))
    if not suffixes:
        return answer
    return f"{answer.rstrip()}\n\n" + "\n\n".join(suffixes)


def _suggest_actions(state: AgentState) -> list[dict[str, Any]]:
    boundary = state["boundary"]["classification"]
    if boundary == "human_handoff_required":
        return [{"label": "转人工客服", "payload": _handoff_payload(state["message"])}]
    if boundary in {"out_of_scope", "security_refusal"}:
        return [{"label": "咨询外设推荐", "payload": {"message": "推荐 300 元以内无线鼠标"}}]
    if any(
        item.disposition == "human_handoff" for item in blocked_subqueries(state.get("route_plan"))
    ):
        return [{"label": "转人工客服", "payload": _handoff_payload(state["message"])}]
    if state.get("products"):
        return [
            {"label": "查询最近订单", "payload": {"message": "帮我查最近订单"}},
            {"label": "继续筛选", "payload": {"message": "帮我进一步筛选这些商品"}},
        ]
    if state.get("order"):
        return [{"label": "转人工处理售后", "payload": {"orderId": state["order"].id}}]
    return []


def _handoff_payload(message: str) -> dict[str, Any]:
    return {
        "handoff": True,
        "orderId": extract_order_id(message),
        "requestType": _handoff_request_type(message),
        "reason": message,
    }


def _handoff_request_type(message: str) -> str:
    if "退款" in message:
        return "refund"
    if "退货" in message or "换货" in message:
        return "return"
    if "维修" in message or "保修" in message:
        return "repair"
    if any(term in message for term in ["取消订单", "改地址", "改收货", "修改订单"]):
        return "order_change"
    return "other"
