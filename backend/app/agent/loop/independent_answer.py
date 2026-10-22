"""Program handoff and tool-free answer with bounded repair and conservative fallback."""

import json

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field, ValidationError

from app.agent.loop.prompts.independent_answer import ANSWER_PROMPT
from app.agent.loop.query_context import resolve_references
from app.agent.loop.query_loop import QuerySession, project_record
from app.agent.loop.query_schema import StrictModel
from app.agent.result_projection import _handoff_payload
from app.core.llm_calls import retryable_transport_error
from app.core.loop_budget import LoopBudget, model_stage
from app.core.loop_tracing import fields, needs_answer_materials, rejection
from app.tools.structured_planning import parse_object


class AnswerInput(StrictModel):
    message: str
    goals: list[dict]
    evidence: list[dict]
    history: list[dict] = Field(default_factory=list)
    stop_reason: str
    action_boundary: str = (
        "已确认能力仅为查询、咨询和引导用户确认人工请求；本轮没有业务写操作或人工请求提交。"
        "不能办理支付、退款或修改订单。其他页面是否支持这些操作未知，"
        "不能建议用户去页面自行办理。可介绍的操作入口仅限本次 suggested_actions；"
        "该列表为空时没有可推荐的操作入口。"
    )
    suggested_actions: list[dict] = Field(default_factory=list)


class AnswerDraft(StrictModel):
    response: str = Field(min_length=1, max_length=12000)
    covered_goal_ids: list[str] = Field(max_length=8)
    evidence_ids: list[str] = Field(max_length=8)


def handoff(session: QuerySession) -> AnswerInput:
    goals = session.book.outcome(session.stop_reason)
    for goal in goals:
        registered = session.book.goals[goal["id"]]
        if registered.references:
            # Goal prose is model-authored; preserve the program's actual identity mapping.
            # This conveys no historical price/stock and does not replace fresh evidence.
            goal["trusted_references"] = resolve_references(session.prepared, registered)
    if not goals:
        goals = [
            {
                "id": "unresolved_request",
                "source_text": session.prepared.message,
                "query": session.prepared.message,
                "disposition": "clarification",
                "execution": {
                    "attempt_count": 0, "ok_count": 0, "error_count": 0, "active_result_count": 0,
                },
                "evidence_ids": [],
                "prerequisites_met": False,
                "stop_reason": session.stop_reason,
            }
        ]
    return AnswerInput(
        message=session.prepared.message,
        goals=goals,
        evidence=[project_record(record, destination="answer")
                  for record in session.adapter.evidence.active_records()],
        history=[item.model_dump(mode="json") for item in session.prepared.history]
        if any(goal["disposition"] == "session_grounded_response" for goal in goals)
        else [],
        stop_reason=session.stop_reason,
        suggested_actions=(
            [{"label": "转人工客服", "payload": _handoff_payload(session.prepared.message)}]
            if any(goal["disposition"] == "human_handoff" for goal in goals)
            else []
        ),
    )


def validate_answer(draft: AnswerDraft, facts: AnswerInput) -> None:
    goals = {goal["id"] for goal in facts.goals}
    if set(draft.covered_goal_ids) != goals or len(draft.covered_goal_ids) != len(goals):
        raise ValueError("answer_must_cover_all_goals")
    allowed = {record["evidence_id"] for record in facts.evidence}
    if not set(draft.evidence_ids) <= allowed or len(set(draft.evidence_ids)) != len(
        draft.evidence_ids
    ):
        raise ValueError("unknown_or_duplicate_answer_reference")
    for goal in facts.goals:
        if goal["evidence_ids"] and not set(goal["evidence_ids"]) & set(draft.evidence_ids):
            raise ValueError("goal_evidence_not_referenced")
    if not draft.response.strip():
        raise ValueError("blank_answer")


def fallback_answer(facts: AnswerInput) -> AnswerDraft:
    records = {r["evidence_id"]: r for r in facts.evidence}
    lines = []
    direct = {
        "security_refusal": "不能提供其他用户的私人信息或系统秘密，只能查询当前登录账号的订单。",
        "human_handoff": "可通过人工请求入口确认并提交诉求。本轮尚未提交，也不代表接单或办理完成。",
        "out_of_scope": "此请求超出当前外设商城咨询范围。",
        "direct_response": (
            "我可以查询商品、本人订单、店铺政策和外设知识，不能办理支付、退款或订单修改。"
        ),
        "session_grounded_response": "暂时未能可靠整理历史对话，请明确需要改写的部分。",
        "clarification": "当前信息不足，请补充或明确这部分要求。",
    }
    for goal in facts.goals:
        source = goal["source_text"]
        lines.append(f"关于“{source[:200]}{'…' if len(source) > 200 else ''}”：")
        if goal["disposition"] != "tool_planning":
            lines.append(direct[goal["disposition"]])
            continue
        for evidence_id in goal["evidence_ids"]:
            record = records[evidence_id]
            output = record["result"].get("output") or {}
            if not record["result"]["ok"]:
                lines.append("有一次查询执行失败，不能据此认定没有结果。")
            elif record["projection_truncated"]:
                lines.append("查询取得了部分材料，但内容超过本次回答窗口，不能据此作完整结论。")
            elif output.get("products"):
                for product in output["products"][:3]:
                    lines.append(f"{product['title'][:120]}，所示版本价格 {product['price']} 元。")
                if len(output["products"]) > 3:
                    lines.append("降级回答仅列出前三个返回候选，请查看卡片中的其余结果。")
                if record["completion_blockers"]:
                    lines.append("仅限返回版本和窗口；存在缺失、范围限制或替代条件，请核对资料。")
            elif output.get("order"):
                order = output["order"]
                lines.append(f"本人订单 {order['id']}：{order['status_label']}。")
            elif output.get("documents"):
                lines.append("已找到相关来源；本次未能可靠整理正文，请查看来源并保留其适用范围。")
            elif (
                output.get("status") in {"no_match", "not_found"}
                or output.get("result_type") == "not_found"
            ):
                lines.append("按本次查询条件未找到匹配对象。")
            else:
                lines.append("本次取得的结果仍需核对其范围与诊断，不能据此作完整结论。")
        if not goal["evidence_ids"]:
            lines.append("尚未取得这部分的查询结果，不能据此作事实判断。")
    return AnswerDraft(
        response="\n".join(lines),
        covered_goal_ids=[g["id"] for g in facts.goals],
        evidence_ids=list(records),
    )


async def answer(facts: AnswerInput, model, budget: LoopBudget) -> tuple[AnswerDraft, str]:
    budget.begin_answer()
    messages = [SystemMessage(content=ANSWER_PROMPT), HumanMessage(content=facts.model_dump_json())]
    repairs = transports = 0
    with model_stage("answer"):
        for _ in range(3):
            response = None
            try:
                response = await model.ainvoke(messages)
                draft = AnswerDraft.model_validate(parse_object(response.content))
                validate_answer(draft, facts)
                return draft, "model"
            except (ValidationError, ValueError) as exc:
                rejection(response.content if response else None, str(exc),
                          {"error_type": type(exc).__name__, "will_repair": not repairs})
                if repairs:
                    break
                repairs += 1
                # Repair metadata is not a new tool result or factual evidence.
                messages = [
                    *messages[:2],
                    HumanMessage(
                        content=json.dumps(
                            {
                                "repair": "Correct JSON, coverage and references; no new facts.",
                                "error_type": type(exc).__name__,
                            }
                        )
                    ),
                ]
            except Exception as exc:
                if transports or not retryable_transport_error(exc):
                    break
                transports += 1
    fields(answer_mode="fallback")
    if needs_answer_materials():
        fields(answer_materials=facts.model_dump(mode="json"))
    return fallback_answer(facts), "fallback"
