"""Fallback answers, terminal templates, and response suggestions."""

import re
from typing import Any

from app.agent.answer_context import build_answer_context
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


def fallback_goal_response(state: AgentState) -> dict[str, Any]:
    """Cover each current goal, preserving facts without claiming a complete synthesis."""
    context = build_answer_context(state)
    tasks = {t["task_id"]: t for t in context["tasks"]}
    lines, used, unavailable = [], [], []
    for goal in context["goals"]:
        lines.append(f"关于“{goal['query'][:100]}”：")
        for supporting in context["supporting_tasks"]:
            if goal["id"] not in supporting["partial_answer_goal_ids"]:
                continue
            source = supporting["artifact"]["source_tool_call_id"]
            if source not in used:
                text, _ = _fallback_task_response(supporting)
                lines.append(text)
                used.append(source)
        if goal["task_ids"]:
            for task_id in goal["task_ids"]:
                task = tasks[task_id]
                text, complete = _fallback_task_response(task)
                lines.append(text)
                if task["resolved"] and task.get("artifact"):
                    used.append(task["artifact"]["source_tool_call_id"])
                if not complete:
                    unavailable.append(task["question"])
        elif goal["disposition"] == "tool_planning":
            lines.append("本次未能完成这部分查询规划，尚未取得查询结果。")
            unavailable.append(goal["query"])
        elif goal["disposition"] == "session_grounded_response":
            lines.append("暂时无法可靠整理前面的对话内容，请稍后重试。")
            unavailable.append(goal["query"])
        elif (
            goal["disposition"] == "direct_response"
            and not _is_safe_direct_request(goal["query"])
        ):
            lines.append("这部分说明暂时未能生成，请稍后重试。")
            unavailable.append(goal["query"])
        else:
            routed = RoutedSubquery.model_validate({
                k: v for k, v in goal.items()
                if k not in {"task_ids", "handling", "blocked_reason"}
            })
            lines.append(_route_notice(routed) or "请补充这部分需求的具体信息。")
            if goal["disposition"] == "clarification":
                unavailable.append(goal["query"])
    return {
        "response": "\n\n".join(lines) or "本次回答未能生成，请稍后重试。",
        "used_tool_call_ids": list(dict.fromkeys(used)),
        "unavailable_parts": list(dict.fromkeys(unavailable)),
        "covered_goal_ids": [g["id"] for g in context["goals"]],
    }


def _fallback_task_response(task: dict[str, Any]) -> tuple[str, bool]:
    outcome = task["semantic_outcome"]
    facts = (task.get("artifact") or {}).get("facts") or {}
    reason = task["result"]["reason_code"]
    if outcome == "needs_clarification":
        missing = task.get("missing_information") or ["具体对象或查询条件"]
        return "请补充或确认：" + "、".join(missing) + "。", False
    if outcome == "answered_no_match":
        return "已完成这项查询，在本次条件和范围内没有找到匹配结果。", True
    if outcome != "answered_with_facts":
        explanations = {
            "execution_failed": "本次查询未能完成，不能据此判断商品或订单是否存在。",
            "temporarily_unavailable": "查询服务暂时不可用，请稍后重试。",
            "unsupported_capability": "当前查询能力暂不支持这部分要求。",
            "blocked_dependency": "前置查询没有提供可用依据，这部分尚未执行。",
            "insufficient_evidence": "现有资料不足以确认这部分结论。",
            "incomplete": "这部分查询尚未完成。",
        }
        if reason == "target_outside_scope":
            return "指定商品不在本次限定范围内，尚未扩大查询范围。", False
        if reason == "target_inactive":
            return "指定商品当前不可用于查询。", False
        if reason == "no_retrieval_evidence":
            return "本次没有检索到支持结论的资料，不代表相关政策或商品能力不存在。", False
        return explanations[outcome], False
    if facts.get("result_type") == "order_count":
        return f"本次查询条件下共有 {facts['total_match_count']} 笔订单。", True
    tool = task["artifact"]["source_tool_name"]
    if tool == "catalog_search" and facts.get("contract_version") == "3":
        material = _catalog_search_fallback(facts)
    elif tool == "catalog_facets":
        material = _catalog_facets_fallback_answer(facts)
        if facts.get("field_issues") or not facts.get("is_exhaustive", True):
            material += " 部分字段缺失、冲突或未完整展示，不能当作全部选项。"
    elif tool == "order_lookup":
        material = "已取得这部分订单资料。"
        order = facts.get("order")
        if order:
            material += f"订单 {order['id']} 当前状态为{order.get('status_label', '待确认')}。"
    else:
        titles = [str(d.get("title") or "相关资料") for d in facts.get("documents", [])]
        material = "已找到可能相关的资料：" + "、".join(titles[:3]) + "。"
    return material + "\n暂时无法可靠生成这部分完整解答，以上仅保留已取得的材料。", False


def _safe_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _fallback_answer(state: AgentState) -> str:
    catalog_output = _latest_successful_tool_output(state, "catalog_search")
    if catalog_output and catalog_output.get("contract_version") == "3":
        return _catalog_search_fallback(catalog_output)
    products = state.get("products", [])
    if products:
        product_search = state.get("parsed", {}).get("product_search", {})
        product_search = product_search if isinstance(product_search, dict) else {}
        usage_mapping = product_search.get("usage_mapping", {})
        usage_status = (
            str(usage_mapping.get("status") or "") if isinstance(usage_mapping, dict) else ""
        )
        result_purpose = str(product_search.get("result_purpose") or "search")
        selection_scope = str(product_search.get("selection_scope") or "spu")
        total_match_count = _safe_non_negative_int(product_search.get("total_match_count"))
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
        if result_purpose != "ranking" and not is_exhaustive and total_match_count > 0:
            lines.append(
                f"共匹配 {total_match_count} 个{scope_label}，"
                f"本次返回 {returned_count or len(products)} 个候选。"
            )
        asks_sales = any(
            term in state.get("message", "").lower() for term in ("销量", "热销", "畅销", "sales")
        )
        for index, product in enumerate(products[:3]):
            is_series = product.entity_scope == "spu" or product.ranking_scope == "spu"
            source_specs = product.series_common_specs if is_series else product.specs
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
                    f"- {role}{product.title}：¥{product.price}，库存 {product.stock}{suffix}。"
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
    if candidates:
        total = int(order_query.get("total_match_count") or len(candidates))
        returned = int(order_query.get("returned_count") or len(candidates))
        is_exhaustive = bool(order_query.get("is_exhaustive", True))
        lines = [f"你一共有 {total} 个订单，本次列出 {returned} 个："]
        lines.extend(
            f"- 订单 {item['id']}：{item['status_label']}，实付 ¥{item['pay_amount']}。"
            for item in candidates
        )
        if not is_exhaustive:
            lines.append("这只是部分结果，不代表全部订单。")
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
    if catalog_output and catalog_output.get("contract_version") == "3":
        return _catalog_search_fallback(catalog_output)
    if catalog_output and _has_catalog_diagnostic(catalog_output, "usage_mapping_unavailable"):
        return (
            "当前目录没有找到能够依据现有规格确认适合这个使用场景的商品。"
            "你可以补充预算、连接方式、重量或最在意的具体规格，我再继续筛选。"
        )
    if "unsupported" in kinds:
        return (
            "这类商品信息我暂时还查不了。"
            "你可以改问具体商品、价格、库存、规格或目录筛选。"
        )
    if "error" in kinds:
        return "业务信息查询暂时失败，请稍后重试。"
    if "not_found" in kinds and tool_names == {"order_lookup"}:
        return "当前账号下没有找到对应订单。请核对订单号，或让我查询最近订单。"
    if tool_names <= {"policy_search", "knowledge_search"}:
        return "当前知识库没有找到足够依据，我不能凭模型常识补写答案。"
    if tool_names <= {"catalog_search", "catalog_facets"}:
        return "当前商品目录没有找到匹配信息。你可以补充型号，或放宽一个筛选条件。"
    return "本次查询没有得到可用于回答的信息，请补充更具体的商品、订单或政策问题。"


def _catalog_search_fallback(output: dict[str, Any]) -> str:
    """Conservative v3 rendering when the answer model cannot summarize evidence."""
    if output.get("query_state") != "completed":
        if output.get("status") == "unsupported":
            return "当前商品查询能力无法处理这项要求，尚未完成匹配，不能据此判断商品是否支持。"
        return "本次商品查询未完成，暂时无法确认匹配结果；请稍后重试。"
    if output.get("status") == "not_found":
        return "当前目录中未定位到该商品，请核对商品名称或编号。"
    if output.get("status") == "no_match":
        return "当前目录中没有找到满足这些条件的商品。可以明确要调整的条件后再查询。"
    if not output.get("products"):
        return "本次未返回商品候选，不能据此判断整个查询没有匹配结果。"
    kind = output.get("match_kind")
    lines = [
        {
            "exact": "找到了满足条件的候选，以下信息仅针对本次返回的版本：",
            "alternative": "未找到严格满足全部条件的商品，以下是存在条件差异的替代候选：",
            "object_facts": "已找到对象资料，但不能确认它满足全部条件：",
        }.get(kind, "查询得到以下商品资料：")
    ]
    for product in output["products"][:3]:
        lines.append(f"- {product.get('spu_title') or product.get('title')}：")
        for check in product.get("condition_checks", []):
            condition = check["condition"]
            if check["verdict"] != "match":
                label = "未满足" if check["verdict"] == "mismatch" else "资料不足，无法确认"
                field_label = CUSTOMER_SPEC_LABELS.get(condition["key"], "该项要求")
                lines.append(
                    f"  一个返回版本的{field_label}：{label}"
                    f"（要求 {condition['values']}，记录值 {check.get('actual') or '缺失'}）。"
                )
        if product.get("field_issues"):
            lines.append("  部分字段资料缺失或冲突，不能解释为商品不支持。")
    if not output.get("matching_complete"):
        lines.append("还有资料不足的版本，匹配结论不完整。")
    if not output.get("is_exhaustive"):
        lines.append("这里只展示本次返回的候选，不代表全部结果。")
    return "\n".join(lines)


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
        unit = {"sku": "个具体版本", "spu": "个商品系列"}.get(item.get("count_scope"))
        suffix = f"（{count} {unit}）" if isinstance(count, int) and unit else ""
        value = item["value"]
        if output.get("facet") == "spec_key":
            value = CUSTOMER_SPEC_LABELS.get(value, "规格项")
            options = [str(v["value"]) for v in item.get("values", []) if v.get("value")]
            if options:
                value += "：" + "、".join(options)
        rendered.append(f"{value}{suffix}")
    if not rendered:
        return f"当前目录中没有找到符合条件的{label}。"
    return (
        f"当前目录中已记录的{label}包括：" + "、".join(rendered)
        + "。选项来自各个实际版本，不代表可以任意组合。"
    )


def _purchase_guidance_answer() -> str:
    return (
        "下单流程可以按这几步走：\n"
        "1. 在商品页确认具体版本、价格、库存和关键规格。\n"
        "2. 选择规格并确认收货信息。\n"
        "3. 提交订单后按页面提示支付，再到订单页查看状态和物流。\n"
        "我不能在聊天中替你提交订单或完成支付。"
    )


def _store_philosophy_answer() -> str:
    return (
        "我这里没有店铺官方理念的介绍，不过可以根据你的预算和用途帮你选商品，"
        "也能帮你查询订单、了解售后政策。"
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
        return subquery.clarification_question.strip() or (
            "请补充或确认：" + "、".join(subquery.missing_information) + "。"
        )
    if subquery.disposition == "direct_response":
        if _is_store_philosophy_question(subquery.query):
            return _store_philosophy_answer()
        if classify_intent(subquery.query) == "purchase_guidance":
            return _purchase_guidance_answer()
        if _is_identity_or_capability_question(subquery.query):
            return (
                "我是小帕瓦，这里的智能助手，可以帮你选键盘、鼠标等外设，查订单和物流，"
                "也能解答选购和售后政策问题。"
            )
        if any(term in subquery.query.casefold() for term in ("谢谢", "thank")):
            return "不客气！"
        if any(term in subquery.query.casefold() for term in ("再见", "bye")):
            return "再见，下次有需要再来找我。"
        return "你好，有什么可以帮你？"
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
    return "你好，有什么可以帮你？"


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
