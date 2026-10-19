"""Task-centered input for the Answer Synthesizer.

The execution runtime keeps route, status, artifact, and ledger structures separate because
they serve scheduling and audit concerns.  The Answer Synthesizer should not have to join those
structures itself, so this module projects them into one user-facing record per routed task.
"""

from collections import Counter
from collections.abc import Mapping
from typing import Any, Literal

from app.agent.limits import MAX_TOOL_WAVES
from app.agent.outcomes import CLARIFICATION_REASONS, is_active_ledger_entry
from app.agent.projections import without_pagination_controls
from app.agent.routing import RequestRoutePlan, tool_planning_subqueries, user_facing_tasks

TaskSemanticOutcome = Literal[
    "answered_with_facts",
    "answered_no_match",
    "needs_clarification",
    "unsupported_capability",
    "temporarily_unavailable",
    "execution_failed",
    "insufficient_evidence",
    "blocked_dependency",
    "incomplete",
]
TurnCompletion = Literal["full", "partial", "none"]
AnswerHandling = Literal["answer", "clarify", "suggest_recovery"]

_RESOLVED_OUTCOMES = {"answered_with_facts", "answered_no_match"}


def build_answer_context(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the answer-only view of the current Task DAG execution."""
    rewritten_query = _route_rewritten_query(state)
    ledger_by_task = _latest_active_ledger_by_task(state)
    artifacts = _mapping(state.get("task_artifacts"))
    statuses = _mapping(state.get("task_status"))
    tasks: list[dict[str, Any]] = []

    for task in user_facing_tasks(state.get("route_plan")):
        status = _mapping(statuses.get(task.id))
        artifact = _mapping(artifacts.get(task.id))
        ledger_entry = ledger_by_task.get(task.id, {})
        semantic_outcome = _semantic_outcome(status, artifact, ledger_entry, state)
        source_tool_call_id = str(
            artifact.get("source_tool_call_id") or ledger_entry.get("tool_call_id") or ""
        )
        source_tool_name = str(
            artifact.get("source_tool_name") or ledger_entry.get("tool_name") or ""
        )
        facts = artifact.get("value")
        result = _result_details(state, source_tool_call_id, facts, ledger_entry)
        handling = _task_handling(semantic_outcome, result, state)
        if source_tool_name in {"catalog_search", "order_lookup"} and isinstance(
            facts, Mapping
        ):
            facts = without_pagination_controls(facts)
        tasks.append(
            {
                "task_id": task.id,
                "goal_id": task.goal_id,
                "question": task.canonical_query,
                "status": str(status.get("status") or "pending"),
                "semantic_outcome": semantic_outcome,
                "handling": handling,
                "result": result,
                "call_arguments": dict(ledger_entry.get("arguments") or {}),
                "recovery": (
                    {"target_stage": "llm2", "reason_code": result["reason_code"],
                     "execution_enabled": False}
                    if handling == "suggest_recovery" else None
                ),
                "missing_information": list(status.get("missing_information") or []),
                "dependencies": [
                    {"task_id": dependency,
                     "status": _mapping(statuses.get(dependency)).get("status", "pending"),
                     "reason": _mapping(statuses.get(dependency)).get("reason", ""),
                     "result_reason": ledger_by_task.get(dependency, {}).get("reason", "")}
                    for dependency in task.depends_on
                ],
                "resolved": semantic_outcome in _RESOLVED_OUTCOMES,
                "artifact": (
                    {
                        "type": str(artifact.get("artifact_type") or task.produces),
                        "facts": facts,
                        "source_tool_call_id": source_tool_call_id,
                        "source_tool_name": source_tool_name,
                    }
                    if artifact or source_tool_call_id
                    else None
                ),
                "response_contract": _response_contract(
                    semantic_outcome,
                    str(artifact.get("artifact_type") or task.produces),
                    facts,
                    source_tool_name,
                    outcome_reason=result["reason_code"],
                ),
                "explanation": _outcome_explanation(
                    semantic_outcome,
                    status,
                    ledger_entry,
                    state,
                ),
            }
        )

    resolved = [task for task in tasks if task["resolved"]]
    unresolved = [task for task in tasks if not task["resolved"]]
    completion: TurnCompletion
    if tasks and not unresolved:
        completion = "full"
    elif resolved:
        completion = "partial"
    else:
        completion = "none"

    answerable_source_ids = list(
        dict.fromkeys(
            str(task["artifact"]["source_tool_call_id"])
            for task in resolved
            if isinstance(task.get("artifact"), Mapping)
            and task["artifact"].get("source_tool_call_id")
        )
    )
    unavailable_parts = [str(task["question"]) for task in unresolved]
    goals = RequestRoutePlan.model_validate(state["route_plan"]).subqueries
    pending_goals = [g for g in goals if g.disposition == "clarification" or g.planning_pending]
    if pending_goals:
        completion = "partial" if resolved else "none"
        unavailable_parts += [g.query for g in pending_goals]
    blocked_goals = {
        g["goal_id"]: g["blocked_reason"]
        for g in state.get("execution_plan", {}).get("blocked_goals", [])
    }
    supporting = _supporting_task_records(state, unresolved, artifacts, statuses, ledger_by_task)
    supporting_source_ids = [
        task["artifact"]["source_tool_call_id"]
        for task in supporting if task["partial_answer_goal_ids"]
    ]
    return {
        "original_message": state.get("message", ""),
        "execution_plan": state.get("execution_plan", {}),
        "supporting_tasks": supporting,
        "supporting_source_tool_call_ids": supporting_source_ids,
        "recovery_policy": _recovery_policy(state),
        "goals": [
            {
                **g.model_dump(mode="json", exclude={"tasks"}),
                "task_ids": [t["task_id"] for t in tasks if t["goal_id"] == g.id],
                # Tool goals are handled per task, so one goal can contain both
                # an answer and a clarification. Blocked plans must not invent tasks.
                "handling": (
                    None if any(t["goal_id"] == g.id for t in tasks)
                    else "clarify" if g.disposition == "clarification" else "answer"
                ),
                "blocked_reason": blocked_goals.get(g.id),
            }
            for g in goals
        ],
        "history": list(state.get("history") or []),
        "blocked_tool_goals": [g for g in state.get("execution_plan", {}).get("blocked_goals", [])],
        "goal_response_policy": "覆盖所有目标。澄清目标在查询后统一提出必要问题，"
        "不声称已完成待确认需求。"
        "直接回复目标也必须回答；安全与人工目标遵守边界，不承诺业务操作已执行。",
        "rewritten_query": rewritten_query,
        "aggregation_contract": {
            "coverage_target": rewritten_query,
            "required": [
                "用 rewritten_query 检查聚合后的回复是否覆盖用户这一轮的完整目标",
                "用各 Task 的 question、semantic_outcome 和 artifact.facts 决定具体回答内容",
            ],
            "forbidden": [
                "把 rewritten_query 当成业务事实来源",
                "用 rewritten_query 覆盖、补写或改变 Task 与 Tool Result",
            ],
        },
        "completion": completion,
        "completion_scope": "tool_evidence_and_pending_goals; not proof of response coverage",
        # A request may be admitted; per-task hints alone never authorize execution.
        "recovery_execution_enabled": _recovery_policy(state)["enabled"],
        "tasks": tasks,
        "resolved_task_ids": [str(task["task_id"]) for task in resolved],
        "unresolved_task_ids": [str(task["task_id"]) for task in unresolved],
        "answerable_source_tool_call_ids": answerable_source_ids,
        "unavailable_parts": unavailable_parts,
        "outcome_counts": dict(Counter(str(task["semantic_outcome"]) for task in tasks)),
        "recommended_control_action": (
            "finish_partial"
            if supporting_source_ids or (pending_goals and resolved)
            else "ask_clarification"
            if completion == "none" and (pending_goals or unresolved)
            and all(g.disposition == "clarification" for g in pending_goals)
            and all(t["handling"] == "clarify" for t in unresolved)
            else _recommended_control_action(completion, unresolved)
        ),
        "late_handoff_policy": (
            "Answer 阶段不得自行决定是否转人工。Runtime 会把 unsupported_capability 确定性地"
            "转换为人工确认入口；其他未完成结果不得转人工。response 不得自行写确认问句或声称"
            "已经转接、提交、记录或办理，固定问句由 Runtime 渲染。"
        ),
    }


def _supporting_task_records(
    state: Mapping[str, Any],
    unresolved: list[dict[str, Any]],
    artifacts: Mapping[str, Any],
    statuses: Mapping[str, Any],
    ledger_by_task: Mapping[str, Mapping[str, Any]],
) -> list[dict]:
    """Expose current prerequisites as optional partial evidence, never completed goals."""
    routed = {t.id: t for t in tool_planning_subqueries(state.get("route_plan"))}
    dependent_goals: dict[str, set[str]] = {}
    for task in unresolved:
        if task["status"] not in {"failed", "unavailable", "blocked"}:
            continue
        pending = list(routed[task["task_id"]].depends_on)
        visited = set()
        while pending:
            identity = pending.pop()
            if identity in visited or identity not in routed:
                continue
            visited.add(identity)
            dependent_goals.setdefault(identity, set()).add(task["goal_id"])
            pending.extend(routed[identity].depends_on)
    records = []
    for task in routed.values():
        if task.answer_role != "internal":
            continue
        artifact = _mapping(artifacts.get(task.id))
        status = _mapping(statuses.get(task.id))
        entry = ledger_by_task.get(task.id, {})
        source = artifact.get("source_tool_call_id")
        semantic = _semantic_outcome(status, artifact, entry, state)
        eligible = (
            status.get("status") == "succeeded" and artifact.get("usable")
            and semantic == "answered_with_facts"
            and source and source == entry.get("tool_call_id")
        )
        facts = artifact.get("value")
        if artifact.get("source_tool_name") in {"catalog_search", "order_lookup"} and isinstance(
            facts, Mapping,
        ):
            facts = without_pagination_controls(facts)
        result = _result_details(state, str(source or ""), facts, entry)
        records.append({
            "task_id": task.id, "goal_id": task.goal_id, "question": task.canonical_query,
            "status": status.get("status", "pending"), "semantic_outcome": semantic,
            "partial_answer_goal_ids": sorted(dependent_goals.get(task.id, ())) if eligible else [],
            "clarification_goal_ids": (
                sorted(dependent_goals.get(task.id, ()))
                if artifact.get("reason") == "order_confirmation_required"
                and source and source == entry.get("tool_call_id") else []
            ),
            "artifact": {"facts": facts, "source_tool_call_id": source,
                         "source_tool_name": artifact.get("source_tool_name")},
            "call_arguments": entry.get("arguments", {}), "result": result,
            "response_contract": _response_contract(
                semantic, str(artifact.get("artifact_type") or task.produces), facts,
                str(artifact.get("source_tool_name") or ""), outcome_reason=result["reason_code"],
            ),
        })
    return records


def _recovery_policy(state: Mapping[str, Any]) -> dict:
    from app.agent.recovery import recovery_enabled

    batch = state.get("recovery_batch", {})
    return {
        "enabled": recovery_enabled(state),
        "max_batches": 1,
        "items": batch.get("items", []),
        "instruction": "只可提出有证据的局部修正，由程序审核；不自动重试原调用。",
    }


def answerable_source_tool_call_ids(state: Mapping[str, Any]) -> list[str]:
    return list(build_answer_context(state)["answerable_source_tool_call_ids"])


def resolved_answer_task_ids(state: Mapping[str, Any]) -> list[str]:
    return list(build_answer_context(state)["resolved_task_ids"])


def _semantic_outcome(
    status: Mapping[str, Any],
    artifact: Mapping[str, Any],
    ledger_entry: Mapping[str, Any],
    state: Mapping[str, Any],
) -> TaskSemanticOutcome:
    task_status = str(status.get("status") or "")
    if task_status == "blocked" and status.get("user_can_supply") is True:
        return "needs_clarification"

    outcome = str(ledger_entry.get("outcome") or "")
    if not outcome:
        status_reason = str(status.get("reason") or "")
        if status_reason.startswith("tool_outcome:"):
            outcome = status_reason.partition(":")[2]
    reason = str(ledger_entry.get("reason") or artifact.get("reason") or "")
    if reason in CLARIFICATION_REASONS:
        return "needs_clarification"
    if outcome == "error":
        code = _tool_error_code(state, str(ledger_entry.get("tool_call_id") or ""))
        if code in {"timeout", "dependency_unavailable", "retrieval_busy"}:
            return "temporarily_unavailable"
        return "execution_failed"
    if artifact.get("usable"):
        return "answered_with_facts"
    if outcome in {"empty", "not_found"}:
        return "answered_no_match"
    if outcome == "unsupported":
        return "unsupported_capability"
    if outcome == "insufficient":
        return "insufficient_evidence"

    if task_status == "blocked":
        return "blocked_dependency"
    if task_status == "failed":
        return "insufficient_evidence"
    if task_status == "unavailable":
        artifact_reason = str(artifact.get("reason") or "")
        if any(marker in artifact_reason for marker in ("no_matching", "not_found")):
            return "answered_no_match"
        return "insufficient_evidence"
    return "incomplete"


def _response_contract(
    outcome: TaskSemanticOutcome,
    artifact_type: str,
    facts: Any,
    source_tool_name: str,
    *,
    outcome_reason: str = "",
) -> dict[str, Any]:
    if outcome == "execution_failed":
        return {
            "required": ["说明本次未能完成查询；按 result.reason_code 区分规划、服务或权限错误"],
            "forbidden": ["声称查无结果、商品资料不足或商品不支持", "泄露错误详情或他人订单信息"],
        }
    if _mapping(facts).get("result_type") == "order_suggestions":
        return {
            "required": ["说明原条件下未找到订单，suggestions只是待确认候选",
                         "按返回顺序展示必要的候选信息及differences，区分不符与无法确认",
                         "询问用户是否指这些订单；suggestion_is_exhaustive=false时说明仅展示部分候选"],
            "forbidden": ["把候选称为完全匹配或已选订单", "擅自继续候选订单的依赖任务",
                          "把候选数量当成严格匹配数量", "将unknown说成不满足条件"],
        }
    if outcome == "needs_clarification":
        return {
            "required": ["围绕缺失信息或冲突条件提出必要问题，保留同轮其他可答结果"],
            "forbidden": ["假设用户未提供的信息", "自行放宽条件"],
        }
    if outcome_reason in {"target_inactive", "target_outside_scope"}:
        return {
            "required": ["说明对象当前不可用或不在指定范围内，依据具体原因区分"],
            "forbidden": ["说成对象不存在或规格资料缺失", "擅自扩大范围"],
        }
    if outcome_reason == "no_retrieval_evidence":
        return {
            "required": ["说明本次没有检索到能支持结论的资料"],
            "forbidden": ["断言本店没有该政策或商品不存在该能力", "用常识补写本店承诺"],
        }
    if source_tool_name == "catalog_search" and _mapping(facts).get("contract_version") == "3":
        data = _mapping(facts)
        kind = data.get("match_kind")
        required = ["按 status/reason_code 说明结果；资料缺失不等于不支持，未执行不等于无匹配。"]
        if kind == "alternative":
            required += [
                "先说明严格无匹配，再按每个SKU的condition_checks说明差异；不代用户接受差异。"
            ]
        elif kind == "object_facts":
            required += ["只说明对象已有事实，逐项指出未满足和未知要求，不声称满足筛选。"]
        elif kind == "exact":
            required += ["严格匹配结果可按其实际事实回答；本次返回为空不等于全店无匹配。"]
        if data.get("result_purpose") == "recommendation" and kind in {"exact", "alternative"}:
            required[-1] = (
                "先说明严格无匹配并按SKU说明差异，不代用户接受；"
                if kind == "alternative" else "只推荐已确认匹配的候选；"
            ) + "沿用products顺序，以ranking_strategy解释排序，不自行重排或把版本销量当型号销量。"
        required += [
            "total_match_count只计严格匹配，candidate_count是候选池，returned_count是本次返回数量；"
            "返回候选少于候选池时说明本次只展示部分结果，不把返回数量当作全部匹配数量。",
            "series_*的数量/价格/库存/共有规格须在对应句、紧邻引言或表头限定为本次候选版本；"
            "model_*仅为全型号数量/价格背景，不证明其他版本满足条件；不得跨SKU拼配置。",
            "缺失或冲突字段依据field_issues解释，不补造用途、静音或兼容性结论。",
        ]
        return {
            "required": required,
            "forbidden": [
                "把替代或对象事实当严格匹配",
                "把候选数量写成该型号有N个版本，或将候选共有规格无范围限定地描述为型号属性",
                "承诺商品翻页、跳页、接着上批往后查或自动排除已展示商品",
            ],
            "result_window": {
                k: data.get(k)
                for k in (
                    "match_kind",
                    "total_match_count",
                    "candidate_count",
                    "returned_count",
                    "matching_complete",
                )
            },
        }
    if outcome == "answered_no_match":
        if outcome_reason == "usage_mapping_unavailable":
            return {
                "required": ["明确说明当前目录没有找到能够依据现有规格确认匹配该场景的商品"],
                "forbidden": [
                    "声称商城没有该品类商品或没有库存",
                    "把结果描述为系统故障、能力不支持或需要人工处理",
                    "编造场景适用性或替代商品",
                ],
            }
        return {
            "required": ["明确说明当前查询没有找到匹配结果"],
            "forbidden": ["把正常空结果描述成系统故障", "编造替代结果"],
        }
    if outcome == "unsupported_capability":
        return {
            "required": ["说明当前工具或数据能力不支持该问题"],
            "forbidden": ["用现有字段推断不受支持的结论"],
        }
    if outcome == "temporarily_unavailable":
        return {
            "required": ["说明对应信息暂时无法查询"],
            "forbidden": ["描述为查无结果或能力不支持"],
        }
    if outcome in {"insufficient_evidence", "blocked_dependency", "incomplete"}:
        return {
            "required": ["说明现有结果不足以支持该问题的结论"],
            "forbidden": ["把无关或不完整结果当成答案"],
        }

    fact_mapping = _mapping(facts)
    if artifact_type == "facets":
        items = [item for item in fact_mapping.get("items", []) if isinstance(item, Mapping)]
        values = [str(item.get("value")) for item in items if item.get("value")]
        count_scopes = {
            str(item.get("count_scope") or "sku") for item in items if item.get("count") is not None
        }
        return {
            "required": [
                "直接回答用户询问的目录选项",
                "列出 items 中的每个 value",
            ],
            "must_include_values": values,
            "count_semantics": (
                "每个 count 按该 item 的 count_scope 统计；spu 表示商品系列数，"
                "sku 表示具体版本数。sku_count 和 spu_count 是两个可核对的明确口径。"
            ),
            "count_scopes": sorted(count_scopes),
            "result_window": {
                key: fact_mapping.get(key)
                for key in ("scope", "total_value_count", "returned_count", "is_exhaustive",
                            "matching_complete", "field_issues")
            },
            "forbidden": [
                "只汇总 count 而省略 value",
                "把 count 总和称为商品系列数或品牌数",
                "忽略缺失或冲突字段，把部分选项当全集或跨SKU拼接规格",
            ],
        }
    if artifact_type in {"products", "ranked_product"}:
        contract = {
            "required": [
                "用返回商品的真实字段回答筛选、推荐或排名问题",
                "推荐理由必须逐项能在商品事实中找到依据",
            ],
            "forbidden": ["补写未返回的规格、用途认证或适配保证"],
        }
        products = [item for item in fact_mapping.get("products", []) if isinstance(item, Mapping)]
        result_purpose = str(fact_mapping.get("result_purpose") or "search")
        selection_scope = str(fact_mapping.get("selection_scope") or "spu")
        query_plan = _mapping(fact_mapping.get("query_plan"))
        relaxation = _mapping(query_plan.get("relaxation"))
        total_match_count = _non_negative_int(fact_mapping.get("total_match_count"))
        returned_count = _non_negative_int(fact_mapping.get("returned_count"))
        is_exhaustive = bool(fact_mapping.get("is_exhaustive", True))
        contract["result_window"] = {
            "result_purpose": result_purpose,
            "selection_scope": selection_scope,
            "total_match_count": total_match_count,
            "returned_count": returned_count,
            "is_exhaustive": is_exhaustive,
        }
        if not is_exhaustive:
            scope_label = "具体版本/SKU" if selection_scope == "sku" else "商品系列/SPU"
            if result_purpose != "ranking":
                contract["required"].append(
                    f"明确共匹配 {total_match_count} 个{scope_label}，本次返回 "
                    f"{returned_count} 个候选，不得把候选数写成全部数量"
                )
            contract["forbidden"].append(f"声称商城只有本次返回的 {returned_count} 个结果")
        if relaxation.get("applied"):
            contract["near_match"] = {
                "mode": relaxation.get("mode"),
                "selection_strategy": relaxation.get("selection_strategy"),
                "within_dimension_sort": relaxation.get("within_dimension_sort"),
                "exact_match_count": relaxation.get("exact_match_count"),
                "requested_filters": relaxation.get("requested_filters") or {},
                "candidate_differences": relaxation.get("candidate_differences") or {},
            }
            contract["required"].extend(
                [
                    "先明确说明没有完全满足全部原始条件的商品，以下仅为接近条件的备选",
                    "逐款依据 near_match.candidate_differences 说明实际规格和被放宽的条件",
                ]
            )
            contract["forbidden"].extend(
                [
                    "声称近似候选完全满足全部原始筛选条件",
                    "隐去近似候选被放宽的条件，或把实际规格改写成用户要求的规格",
                ]
            )
        if result_purpose == "recommendation" and relaxation.get("applied"):
            contract["required"].append(
                "把候选作为不同放宽维度的权衡方案；如给出首选，必须说明依据及其实际偏离项"
            )
            contract["forbidden"].append(
                "声称近似候选是全局按销量排序；候选仅在同一放宽维度内参考销量"
            )
        elif result_purpose == "recommendation":
            contract["required"].append(
                "products 已按推荐顺序排列；直接把第一项作为首选，其余项只作为备选"
            )
        elif result_purpose == "search":
            contract["required"].append("把 products 表达为搜索结果，不得把第一项自动称为最推荐")
        elif result_purpose == "lookup":
            contract["required"].append(
                "围绕已识别商品回答事实或版本信息，不得把返回窗口误称为完整商品集合"
            )
        elif result_purpose == "ranking":
            contract["required"].append("按 query_plan.ranking 与商品 ranking_value 回答确定性名次")
        if any(
            item.get("entity_scope") == "spu" or item.get("ranking_scope") == "spu"
            for item in products
        ):
            contract["required"].extend(
                [
                    "把返回对象作为商品系列/SPU，使用 spu_title 作为系列名称",
                    "价格使用 series_min_price/series_max_price，库存使用 series_total_stock",
                    "规格只使用 series_common_specs、series_option_specs 和 series_variants",
                ]
            )
            if any(item.get("ranking_scope") == "spu" for item in products):
                contract["required"].append(
                    "明确这是商品系列/SPU 排名，并使用 ranking_value 说明排名依据"
                )
            contract["forbidden"].extend(
                [
                    "把辅助 SKU 的 stock 当成系列总库存",
                    "把辅助 SKU 的 specs 当成全系列共同规格",
                    "把辅助 SKU 的 title 当成系列名称（有 spu_title 时使用 spu_title）",
                ]
            )
        return contract
    if artifact_type == "order":
        facts = fact_mapping
        query_mode = str(facts.get("query_mode") or "")
        total = facts.get("total_match_count")
        returned = facts.get("returned_count")
        coverage_gate = {
            "provided_fields": [
                "订单号",
                "订单状态",
                "订单总金额",
                "实付金额",
                "下单时间",
                "商品数量",
                "首个商品名称",
                "物流单号",
                "商品明细（仅单笔订单或 analysis_orders）",
                "运费、支付/发货时间（仅单笔订单或 analysis_orders）",
                "物流公司、状态和轨迹（仅单笔订单或 analysis_orders）",
            ],
            "not_provided_fields": [
                "收件人姓名",
                "收件人手机号",
                "收货地址",
                "支付方式",
                "预计送达时间或还需多久到货",
                "批量地址表或导出文件",
            ],
            "priority": (
                "先判断该任务的字段覆盖；不得据此丢弃其他目标的有效答案"
            ),
            "on_missing_core_field": (
                "如果 Task question 的核心目标属于 not_provided_fields，忽略后续订单摘要展示要求；"
                "说明该任务的核心信息未提供，不把其他字段当作它的答案。仅当全部工具任务都是"
                "订单且都无法回答时，调用 finish_unavailable 并设置"
                " observation_coverage_failure=true；"
                "混合请求仍保留其他任务和非工具目标的回答。"
            ),
        }

        def order_contract(
            required: list[str],
            forbidden: list[str],
        ) -> dict[str, Any]:
            return {
                "coverage_gate": coverage_gate,
                "required": required,
                "forbidden": forbidden,
            }

        if query_mode == "count":
            return order_contract(
                [f"明确回答用户共有 {total or 0} 个订单"],
                [
                    "把订单候选列表数量当成总订单数",
                    "声称修改、取消、催促或办理了订单操作",
                ],
            )
        if query_mode == "analysis":
            exhaustive = bool(facts.get("is_exhaustive", False))
            required = [
                "把 analysis_orders 仅作为分析当前问题的证据，只回答用户实际询问的结论",
                "只引用支撑结论所必需的订单、商品、状态、时间或物流字段",
                "根据订单商品名称及规格识别用户提到的相关商品，不要求逐字完全相同",
            ]
            if not exhaustive:
                required.append("明确说明当前订单事实不完整，不能给出覆盖全部订单的否定结论")
            return order_contract(
                required,
                [
                    "把 analysis_orders 中的全部订单逐项复述给用户",
                    "展示与用户问题无关的订单号、商品、金额、状态或物流",
                    "把发货时间或物流轨迹事件时间解释成预计送达时间",
                    "在 is_exhaustive=false 时声称用户从未买过某商品或不存在某类订单",
                    "声称修改、取消、催促或办理了订单操作",
                ],
            )
        if facts.get("candidates"):
            required = [
                f"明确说明用户共有 {total or 0} 个订单",
                f"列出本次返回的 {returned or 0} 个订单摘要，不自动选择第一单",
                (
                    "只使用订单摘要实际提供的订单号、状态、订单总金额、实付金额、"
                    "下单时间、商品数量、首个商品名称、物流公司、物流单号和最新物流事件"
                ),
            ]
            if not facts.get("is_exhaustive", True):
                required.append("明确说明这只是部分结果，不承诺继续返回未展示的订单")
            return order_contract(
                required,
                [
                    "把本次返回数量说成总订单数",
                    "Observation 未提供收件人姓名、手机号或收货地址时声称已经回答这些字段",
                    "把物流轨迹事件时间解释成预计送达时间",
                    "声称修改、取消、催促或办理了订单操作",
                ],
            )
        return order_contract(
            ["明确回答订单、状态、明细或物流问题"],
            ["声称修改、取消、催促或办理了订单操作"],
        )
    if artifact_type == "documents":
        return {
            "required": ["判断返回片段是否回答问题，再据来源归纳；相关性分数不证明结论正确"],
            "forbidden": ["用模型常识补写政策、时限或承诺", "把外部网页当作本店政策或订单事实"],
        }
    return {
        "required": ["直接回答当前 Task 的 question"],
        "forbidden": [f"把 {source_tool_name or 'Tool'} 的无关字段当成核心答案"],
    }


def _outcome_explanation(
    outcome: TaskSemanticOutcome,
    status: Mapping[str, Any],
    ledger_entry: Mapping[str, Any],
    state: Mapping[str, Any],
) -> str:
    explanations = {
        "answered_with_facts": "已取得可用材料；仍需检查是否覆盖所问内容及其限制。",
        "answered_no_match": "Tool 正常完成查询，但没有找到匹配结果；这是可靠的否定结论。",
        "needs_clarification": "缺少用户能够补充的必要信息。",
        "unsupported_capability": "Tool 已确认当前数据或查询能力不能支持该问题。",
        "temporarily_unavailable": "Tool 或其依赖暂时不可用。",
        "execution_failed": "本次规划或执行失败，不能据此判断商品、订单或政策事实。",
        "insufficient_evidence": "当前 Observation 不足以支持可靠结论。",
        "blocked_dependency": "前置 Task 没有产生可用 Artifact，后续 Task 无法执行。",
        "incomplete": "Task 尚未形成可用于回答的终态。",
    }
    base = explanations[outcome]
    reason = str(ledger_entry.get("reason") or "")
    if reason in {"target_inactive", "target_outside_scope"}:
        return "指定对象当前不可用于查询或不在本次范围内，按具体原因解释，不改变范围。"
    if reason == "no_retrieval_evidence":
        return "本次检索没有取得依据，不代表政策或商品能力不存在。"
    if (
        outcome == "answered_no_match"
        and str(ledger_entry.get("reason") or "") == "usage_mapping_unavailable"
    ):
        return (
            "当前目录没有能依据现有规格确认匹配该场景的商品；这是限定于场景证明条件的可靠否定结论。"
        )
    missing = status.get("missing_information")
    if outcome == "needs_clarification" and isinstance(missing, list) and missing:
        return f"{base} 需要补充：{'、'.join(str(item) for item in missing)}。"
    if outcome in {"temporarily_unavailable", "execution_failed"}:
        code = _tool_error_code(state, str(ledger_entry.get("tool_call_id") or ""))
        return f"{base} 错误类型：{code or reason or 'execution_error'}。"
    return base


def _result_details(
    state: Mapping[str, Any], call_id: str, facts: Any, ledger: Mapping[str, Any],
) -> dict[str, Any]:
    result = next(
        (r for r in reversed(state.get("tool_results", []))
         if isinstance(r, Mapping) and r.get("tool_call_id") == call_id),
        {},
    )
    execution = _mapping(result.get("execution"))
    output = _mapping(execution.get("output")) or _mapping(facts)
    error = _mapping(execution.get("error"))
    reason = str(error.get("code") or ledger.get("reason") or output.get("reason_code") or "")
    diagnostic = next(
        (d for d in output.get("diagnostics", [])
         if isinstance(d, Mapping) and d.get("code") == reason),
        {},
    )
    return {
        "reason_code": reason.removeprefix("tool_execution_failed:"),
        "execution_ok": execution.get("ok"),
        "query_state": output.get("query_state", "unknown"),
        "tool_status": output.get("status", output.get("result_type")),
        "recommended_action": error.get("recommended_action") or output.get("recommended_action")
        or diagnostic.get("recommended_action"),
        "retryable": error.get("retryable", output.get("retryable", False)),
        "phase": error.get("phase") or _mapping(diagnostic.get("details")).get("phase"),
        "error_details": dict(_mapping(error.get("details"))),
        "diagnostics": list(output.get("diagnostics") or []),
        "field_issues": list(output.get("field_issues") or []),
    }


def _task_handling(
    outcome: TaskSemanticOutcome, result: Mapping[str, Any], state: Mapping[str, Any],
) -> AnswerHandling:
    if outcome == "needs_clarification":
        return "clarify"
    # This is an advisory classification, not an execution permission. Never turn
    # exhausted internal planning or timeout retries into a fresh planning budget.
    if state.get("tool_wave_count", 0) < MAX_TOOL_WAVES and (
        (result["reason_code"] == "multiple_targets"
         and result["recommended_action"] == "split_tasks")
        or (result["reason_code"] == "product_identity_required"
            and result["recommended_action"] == "resolve_product_with_catalog_search")
        or (result["reason_code"] == "invalid_input" and result["retryable"]
            and result["recommended_action"] == "replan_arguments")
    ):
        return "suggest_recovery"
    return "answer"


def _recommended_control_action(
    completion: TurnCompletion,
    unresolved: list[dict[str, Any]],
) -> str:
    if completion == "full":
        return "finish_answer"
    if completion == "partial":
        return "finish_partial"
    if unresolved and all(task["semantic_outcome"] == "needs_clarification" for task in unresolved):
        return "ask_clarification"
    return "finish_unavailable"


def _latest_active_ledger_by_task(
    state: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for entry in state.get("subquery_ledger", []):
        if not isinstance(entry, Mapping) or not is_active_ledger_entry(entry):
            continue
        task_id = str(entry.get("subquery") or "").strip()
        if task_id:
            result[task_id] = entry
    return result


def _tool_error_code(state: Mapping[str, Any], call_id: str) -> str:
    for result in reversed(state.get("tool_results", [])):
        if not isinstance(result, Mapping):
            continue
        if str(result.get("tool_call_id") or "") != call_id:
            continue
        execution = _mapping(result.get("execution"))
        error = _mapping(execution.get("error"))
        return str(error.get("code") or "")
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _route_rewritten_query(state: Mapping[str, Any]) -> str:
    route_plan = state.get("route_plan")
    if isinstance(route_plan, Mapping):
        return str(route_plan.get("rewritten_query") or "").strip()
    return str(getattr(route_plan, "rewritten_query", "") or "").strip()


__all__ = [
    "answerable_source_tool_call_ids",
    "build_answer_context",
    "resolved_answer_task_ids",
]
