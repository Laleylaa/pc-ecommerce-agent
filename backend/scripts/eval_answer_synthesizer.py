"""Live behavioral evaluation for the task-centered Answer Synthesizer.

Run from ``backend`` with:

    .venv/bin/python -m scripts.eval_answer_synthesizer

The script uses the configured chat model but only simulated Tool observations. It does not
connect to the catalog, order database, Redis, or vector store.
"""

import argparse
import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.answer_context import build_answer_context
from app.agent.graph import AgentRuntime
from app.agent.responses import LATE_HANDOFF_CONFIRMATION
from app.agent.routing import RequestRoutePlan
from app.agent.state import AgentState
from app.core.config import get_settings

Check = Callable[[str, dict[str, Any]], bool]


def _task(
    task_id: str,
    goal_id: str,
    question: str,
    *,
    produces: str,
    capability: str,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "goal_id": goal_id,
        "canonical_query": question,
        "depends_on": [],
        "input_requirements": [],
        "produces": produces,
        "answer_role": "user_facing",
        "capability": capability,
    }


def _route_plan(rewritten_query: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    goals = [
        {
            "id": task["goal_id"],
            "query": task["canonical_query"],
            "disposition": "tool_planning",
            "reason_code": "answer_live_eval",
            "tasks": [task],
        }
        for task in tasks
    ]
    return RequestRoutePlan(
        rewritten_query=rewritten_query,
        subqueries=goals,
    ).model_dump(mode="json")


def _artifact(
    task: dict[str, Any],
    call_id: str,
    tool_name: str,
    *,
    usable: bool,
    value: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "task_id": task["id"],
        "goal_id": task["goal_id"],
        "artifact_type": task["produces"],
        "usable": usable,
        "value": value,
        "evidence": (
            [{"source_tool_call_id": call_id, "source_tool_name": tool_name}]
            if usable
            else []
        ),
        "source_tool_call_id": call_id,
        "source_tool_name": tool_name,
        "extractor": "deterministic",
        "reason": reason,
    }


def _ledger(
    task: dict[str, Any],
    call_id: str,
    tool_name: str,
    outcome: str,
) -> dict[str, Any]:
    usable = outcome == "usable"
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "subquery": task["id"],
        "status": "ready_to_answer" if usable else "unavailable",
        "outcome": outcome,
        "has_usable_information": usable,
        "reason": f"live_eval:{outcome}",
        "wave": 1,
        "arguments": {},
    }


def _tool_result(
    call_id: str,
    tool_name: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tool_call_id": call_id,
        "name": tool_name,
        "execution": {
            "tool_name": tool_name,
            "ok": True,
            "output": output,
            "error": None,
        },
    }


def _state(
    message: str,
    rewritten_query: str,
    tasks: list[dict[str, Any]],
    *,
    statuses: dict[str, dict[str, Any]],
    artifacts: dict[str, dict[str, Any]],
    ledger: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> AgentState:
    return cast(
        AgentState,
        {
            "message": message,
            "route_plan": _route_plan(rewritten_query, tasks),
            "task_status": statuses,
            "task_artifacts": artifacts,
            "subquery_ledger": ledger,
            "tool_results": tool_results,
            "tool_waves": [],
            "tool_wave_count": 1 if tool_results else 0,
            "orchestrator_call_count": 1,
            "boundary": {
                "classification": "in_scope_auto",
                "reason": "live answer evaluation",
                "display_message": "可自动回答",
            },
            "intent": "answer_live_eval",
            "products": [],
            "evidence": [],
            "order": None,
            "blocked_subqueries": [],
        },
    )


def _single_usable_state(
    *,
    message: str,
    rewritten_query: str,
    question: str,
    produces: str,
    capability: str,
    tool_name: str,
    call_id: str,
    artifact_value: dict[str, Any],
    tool_output: dict[str, Any],
) -> AgentState:
    task = _task(
        "task_1",
        "goal_1",
        question,
        produces=produces,
        capability=capability,
    )
    return _state(
        message,
        rewritten_query,
        [task],
        statuses={"task_1": {"status": "succeeded"}},
        artifacts={
            "task_1": _artifact(
                task,
                call_id,
                tool_name,
                usable=True,
                value=artifact_value,
                reason="live_eval_usable",
            )
        },
        ledger=[_ledger(task, call_id, tool_name, "usable")],
        tool_results=[_tool_result(call_id, tool_name, tool_output)],
    )


def _single_unavailable_state(
    *,
    message: str,
    rewritten_query: str,
    question: str,
    produces: str,
    capability: str,
    tool_name: str,
    call_id: str,
    outcome: str,
    reason: str,
    tool_output: dict[str, Any],
) -> AgentState:
    task = _task(
        "task_1",
        "goal_1",
        question,
        produces=produces,
        capability=capability,
    )
    return _state(
        message,
        rewritten_query,
        [task],
        statuses={
            "task_1": {
                "status": "unavailable",
                "reason": f"tool_outcome:{outcome}",
            }
        },
        artifacts={
            "task_1": _artifact(
                task,
                call_id,
                tool_name,
                usable=False,
                value=None,
                reason=reason,
            )
        },
        ledger=[_ledger(task, call_id, tool_name, outcome)],
        tool_results=[_tool_result(call_id, tool_name, tool_output)],
    )


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)


def _uses_plain_customer_language(text: str, _: dict[str, Any]) -> bool:
    forbidden = ("SKU", "SPU", "sku_id", "spu_id", "Artifact", "semantic_outcome")
    return not _contains_any(text, forbidden)


def _cases() -> list[dict[str, Any]]:
    facet_task = _task(
        "task_1",
        "goal_1",
        "查询键盘有哪些品牌",
        produces="facets",
        capability="catalog_facets",
    )
    facet_items = [
        {"value": "Akko", "count": 96},
        {"value": "Keychron", "count": 96},
        {"value": "Razer", "count": 96},
        {"value": "Wooting", "count": 96},
    ]
    facet_state = _state(
        "你好，有什么牌子的键盘？",
        "查询商城键盘品类当前包含的品牌",
        [facet_task],
        statuses={"task_1": {"status": "succeeded"}},
        artifacts={
            "task_1": _artifact(
                facet_task,
                "facets-1",
                "catalog_facets",
                usable=True,
                value={"facet": "brand", "items": facet_items},
                reason="facets_found",
            )
        },
        ledger=[_ledger(facet_task, "facets-1", "catalog_facets", "usable")],
        tool_results=[
            _tool_result(
                "facets-1",
                "catalog_facets",
                {"result_type": "facets", "facet": "brand", "items": facet_items},
            )
        ],
    )

    product_task = _task(
        "task_1",
        "goal_1",
        "推荐一款 500 元以内的无线办公鼠标",
        produces="products",
        capability="catalog_search",
    )
    policy_task = _task(
        "task_2",
        "goal_2",
        "说明商城退货时限",
        produces="documents",
        capability="policy_search",
    )
    product = {
        "spu_id": 10,
        "sku_id": 101,
        "title": "MouseAir 2 无线鼠标",
        "brand": "Example",
        "category": "mouse",
        "price": "399.00",
        "stock": 12,
        "sku_sales_count": 31,
        "sales_count": 88,
        "specs": {"connection_type": "2.4G / Bluetooth"},
    }
    document = {
        "source_type": "policy_document",
        "source_id": 7,
        "title": "退货政策",
        "document_type": "policy",
        "snippet": "符合条件的商品可在签收后七天内申请退货。",
    }
    aggregate_state = _state(
        "推荐一个无线办公鼠标，再告诉我多久可以退货。",
        "推荐 500 元以内的无线办公鼠标，并说明商城退货时限",
        [product_task, policy_task],
        statuses={
            "task_1": {"status": "succeeded"},
            "task_2": {"status": "succeeded"},
        },
        artifacts={
            "task_1": _artifact(
                product_task,
                "search-1",
                "catalog_search",
                usable=True,
                value={"products": [product], "query_plan": {}},
                reason="products_found",
            ),
            "task_2": _artifact(
                policy_task,
                "policy-1",
                "policy_search",
                usable=True,
                value={"documents": [document]},
                reason="documents_found",
            ),
        },
        ledger=[
            _ledger(product_task, "search-1", "catalog_search", "usable"),
            _ledger(policy_task, "policy-1", "policy_search", "usable"),
        ],
        tool_results=[
            _tool_result(
                "search-1",
                "catalog_search",
                {"result_type": "products", "products": [product]},
            ),
            _tool_result(
                "policy-1",
                "policy_search",
                {"result_type": "documents", "documents": [document]},
            ),
        ],
    )

    trend_task = _task(
        "task_2",
        "goal_2",
        "分析这款键盘过去一年的销量增长趋势",
        produces="products",
        capability="catalog_search",
    )
    keyboard = {
        "spu_id": 20,
        "sku_id": 201,
        "title": "QuietBoard 75 无线键盘",
        "brand": "Example",
        "category": "keyboard",
        "price": "459.00",
        "stock": 9,
        "sku_sales_count": 18,
        "sales_count": 56,
        "specs": {"connection_type": "2.4G / Bluetooth", "switches": "静音红轴"},
    }
    partial_state = _state(
        "推荐无线办公键盘，再分析一下过去一年的销量增长。",
        "推荐无线办公键盘，并分析推荐商品过去一年的销量增长趋势",
        [product_task := _task(
            "task_1",
            "goal_1",
            "推荐一款无线办公键盘",
            produces="products",
            capability="catalog_search",
        ), trend_task],
        statuses={
            "task_1": {"status": "succeeded"},
            "task_2": {"status": "unavailable", "reason": "tool_outcome:unsupported"},
        },
        artifacts={
            "task_1": _artifact(
                product_task,
                "keyboard-1",
                "catalog_search",
                usable=True,
                value={"products": [keyboard], "query_plan": {}},
                reason="products_found",
            ),
            "task_2": _artifact(
                trend_task,
                "trend-1",
                "catalog_search",
                usable=False,
                value=None,
                reason="query_not_supported_by_tool",
            ),
        },
        ledger=[
            _ledger(product_task, "keyboard-1", "catalog_search", "usable"),
            _ledger(trend_task, "trend-1", "catalog_search", "unsupported"),
        ],
        tool_results=[
            _tool_result(
                "keyboard-1",
                "catalog_search",
                {"result_type": "products", "products": [keyboard]},
            ),
            _tool_result(
                "trend-1",
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "diagnostics": [{"code": "unsupported_query"}],
                },
            ),
        ],
    )

    no_match_task = _task(
        "task_1",
        "goal_1",
        "查询 10 元以内的 4K 显示器",
        produces="products",
        capability="catalog_search",
    )
    no_match_state = _state(
        "有没有十块钱以内的 4K 显示器？",
        "查询商城是否有价格不超过 10 元的 4K 显示器",
        [no_match_task],
        statuses={"task_1": {"status": "unavailable", "reason": "tool_outcome:empty"}},
        artifacts={
            "task_1": _artifact(
                no_match_task,
                "empty-1",
                "catalog_search",
                usable=False,
                value=None,
                reason="no_matching_products",
            )
        },
        ledger=[_ledger(no_match_task, "empty-1", "catalog_search", "empty")],
        tool_results=[
            _tool_result(
                "empty-1",
                "catalog_search",
                {"result_type": "empty", "products": []},
            )
        ],
    )

    unsupported_task = _task(
        "task_1",
        "goal_1",
        "分析键盘品类过去三年的月度销量增长率",
        produces="products",
        capability="catalog_search",
    )
    unsupported_state = _state(
        "分析一下键盘过去三年的月度销量增长率。",
        "分析商城键盘品类过去三年的月度销量增长率",
        [unsupported_task],
        statuses={
            "task_1": {"status": "unavailable", "reason": "tool_outcome:unsupported"}
        },
        artifacts={
            "task_1": _artifact(
                unsupported_task,
                "unsupported-1",
                "catalog_search",
                usable=False,
                value=None,
                reason="query_not_supported_by_tool",
            )
        },
        ledger=[
            _ledger(
                unsupported_task,
                "unsupported-1",
                "catalog_search",
                "unsupported",
            )
        ],
        tool_results=[
            _tool_result(
                "unsupported-1",
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "diagnostics": [{"code": "unsupported_query"}],
                },
            )
        ],
    )

    clarification_task = _task(
        "task_1",
        "goal_1",
        "查询用户指定订单的物流状态",
        produces="order",
        capability="order_lookup",
    )
    clarification_state = _state(
        "帮我查一下物流。",
        "查询用户想了解的订单物流状态",
        [clarification_task],
        statuses={
            "task_1": {
                "status": "blocked",
                "reason": "missing_context_artifact:order_id",
                "missing_information": ["订单号或需要查询的具体订单"],
                "user_can_supply": True,
            }
        },
        artifacts={},
        ledger=[],
        tool_results=[],
    )

    handoff_task = _task(
        "task_1",
        "goal_1",
        "确认是否需要人工协助办理退货",
        produces="documents",
        capability="policy_search",
    )
    handoff_state = _state(
        "这个退货能不能找人帮我处理一下？",
        "确认用户是否希望由人工客服协助办理退货",
        [handoff_task],
        statuses={
            "task_1": {"status": "unavailable", "reason": "tool_outcome:unsupported"}
        },
        artifacts={
            "task_1": _artifact(
                handoff_task,
                "handoff-1",
                "policy_search",
                usable=False,
                value=None,
                reason="query_not_supported_by_tool",
            )
        },
        ledger=[_ledger(handoff_task, "handoff-1", "policy_search", "unsupported")],
        tool_results=[
            _tool_result(
                "handoff-1",
                "policy_search",
                {
                    "result_type": "empty",
                    "documents": [],
                    "diagnostics": [{"code": "unsupported_query"}],
                },
            )
        ],
    )

    return [
        {
            "name": "facets_lists_brands",
            "state": facet_state,
            "expected_action": "finish_answer",
            "checks": [
                (
                    "lists_every_brand",
                    lambda text, _: all(item["value"] in text for item in facet_items),
                ),
                ("does_not_repeat_384_bug", lambda text, _: "384 款" not in text),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "rewritten_query_aggregates_two_tasks",
            "state": aggregate_state,
            "expected_action": "finish_answer",
            "checks": [
                ("covers_product", lambda text, _: "MouseAir 2" in text),
                (
                    "covers_return_window",
                    lambda text, _: _contains_any(text, ("七天", "7 天", "7天")),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "partial_answers_supported_part",
            "state": partial_state,
            "expected_action": "finish_partial",
            "checks": [
                ("keeps_supported_product", lambda text, _: "QuietBoard 75" in text),
                ("mentions_unanswered_trend", lambda text, _: "趋势" in text),
                (
                    "explains_capability_limit",
                    lambda text, _: _contains_any(text, ("不支持", "无法", "不能", "缺少")),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "no_match_is_complete_negative_answer",
            "state": no_match_state,
            "expected_action": "finish_answer",
            "checks": [
                (
                    "states_no_match",
                    lambda text, _: _contains_any(text, ("没有找到", "未找到", "暂无")),
                ),
                (
                    "does_not_call_it_system_failure",
                    lambda text, _: not _contains_any(text, ("系统故障", "系统异常")),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "unsupported_explains_limit",
            "state": unsupported_state,
            "expected_action": "finish_unavailable",
            "checks": [
                (
                    "explains_unsupported",
                    lambda text, _: _contains_any(text, ("不支持", "无法", "不能")),
                ),
                ("does_not_invent_trend", lambda text, _: "%" not in text),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "missing_order_asks_one_clarification",
            "state": clarification_state,
            "expected_action": "ask_clarification",
            "checks": [
                (
                    "asks_for_order",
                    lambda text, _: "订单" in text
                    and _contains_any(text, ("订单号", "哪一笔", "具体订单")),
                ),
                (
                    "is_a_question",
                    lambda text, _: _contains_any(text, ("？", "?", "请提供", "请告诉")),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "late_handoff_only_asks_confirmation",
            "state": handoff_state,
            "expected_action": "finish_unavailable",
            "expected_offer_handoff_confirmation": True,
            "checks": [
                (
                    "safe_handoff_question",
                    lambda text, _: text.endswith(LATE_HANDOFF_CONFIRMATION),
                ),
                (
                    "boundary_stays_automatic",
                    lambda _text, result: result["boundary"] == "in_scope_auto",
                ),
                (
                    "no_frontend_handoff_action",
                    lambda _text, result: result["suggested_actions"] == [],
                ),
            ],
        },
    ]


def _normal_guard_cases() -> list[dict[str, Any]]:
    comparison_products = [
        {
            "spu_id": 31,
            "sku_id": 301,
            "title": "SwiftMouse 无线鼠标",
            "brand": "Example",
            "category": "mouse",
            "price": "299.00",
            "stock": 16,
            "sku_sales_count": 42,
            "sales_count": 110,
            "specs": {"connection_type": "2.4G", "weight_g": 68},
        },
        {
            "spu_id": 32,
            "sku_id": 302,
            "title": "ErgoMouse Pro 无线鼠标",
            "brand": "Example",
            "category": "mouse",
            "price": "459.00",
            "stock": 7,
            "sku_sales_count": 27,
            "sales_count": 79,
            "specs": {
                "connection_type": "2.4G / Bluetooth",
                "weight_g": 92,
            },
        },
    ]
    comparison_state = _single_usable_state(
        message="SwiftMouse 和 ErgoMouse Pro 有什么区别？",
        rewritten_query="比较 SwiftMouse 与 ErgoMouse Pro 的价格、连接方式和重量",
        question="比较两款无线鼠标的价格、连接方式和重量",
        produces="comparison",
        capability="catalog_compare",
        tool_name="catalog_compare",
        call_id="compare-normal-1",
        artifact_value={
            "products": comparison_products,
            "comparison_fields": ["price", "connection_type", "weight_g"],
        },
        tool_output={
            "result_type": "comparison",
            "products": comparison_products,
            "comparison_fields": ["price", "connection_type", "weight_g"],
        },
    )

    order = {
        "id": 202607230001,
        "status": 3,
        "status_label": "已发货",
        "pay_amount": "399.00",
        "created_at": "2026-07-22T10:00:00",
        "items": [{"title": "MouseAir 2 无线鼠标", "quantity": 1}],
        "logistics": {
            "company": "顺丰速运",
            "tracking_no": "SF1234567890",
            "status": "运输中",
        },
    }
    order_state = _single_usable_state(
        message="帮我看看订单 202607230001 到哪了。",
        rewritten_query="查询当前用户订单 202607230001 的状态和物流",
        question="查询订单 202607230001 的状态和物流",
        produces="order",
        capability="order_lookup",
        tool_name="order_lookup",
        call_id="order-normal-1",
        artifact_value={
            "order": order,
            "candidates": [],
            "result_type": "single_order",
        },
        tool_output={"result_type": "single_order", "order": order},
    )

    return_document = {
        "source_type": "policy_document",
        "source_id": 21,
        "title": "退货条件",
        "document_type": "policy",
        "snippet": "商品签收后七天内、包装和配件完整且不影响二次销售时，可以在线申请退货。",
    }
    return_policy_state = _single_usable_state(
        message="我不需要人工，只想了解一下退货要满足什么条件。",
        rewritten_query="说明商城退货需要满足的条件，不申请人工客服",
        question="说明商城退货需要满足的条件",
        produces="documents",
        capability="policy_search",
        tool_name="policy_search",
        call_id="policy-normal-1",
        artifact_value={"documents": [return_document]},
        tool_output={"result_type": "documents", "documents": [return_document]},
    )

    review_document = {
        "source_type": "policy_document",
        "source_id": 22,
        "title": "退货审核流程",
        "document_type": "policy",
        "snippet": "用户可先在订单页面提交退货申请，提交后由售后人员审核，无需先转接人工客服。",
    }
    review_policy_state = _single_usable_state(
        message="退货是不是一定要人工审核？我只是问流程。",
        rewritten_query="说明退货申请是否需要人工审核以及申请流程",
        question="说明退货申请的审核和提交方式",
        produces="documents",
        capability="policy_search",
        tool_name="policy_search",
        call_id="policy-normal-2",
        artifact_value={"documents": [review_document]},
        tool_output={"result_type": "documents", "documents": [review_document]},
    )

    warranty_document = {
        "source_type": "policy_document",
        "source_id": 23,
        "title": "键盘保修申请",
        "document_type": "policy",
        "snippet": "键盘出现非人为质量问题时，可在订单售后入口上传故障说明和购买凭证申请保修。",
    }
    warranty_state = _single_usable_state(
        message="键盘按键失灵了，怎么申请保修？",
        rewritten_query="说明键盘按键失灵时申请保修的流程和材料",
        question="说明键盘质量问题的保修申请流程",
        produces="documents",
        capability="policy_search",
        tool_name="policy_search",
        call_id="policy-normal-3",
        artifact_value={"documents": [warranty_document]},
        tool_output={"result_type": "documents", "documents": [warranty_document]},
    )

    switch_document = {
        "source_type": "knowledge_document",
        "source_id": 31,
        "title": "机械键盘轴体区别",
        "document_type": "guide",
        "snippet": "红轴手感较轻且声音较小，适合办公和长时间输入；青轴段落感明显、声音更响。",
    }
    switch_state = _single_usable_state(
        message="红轴和青轴有什么区别，办公室更适合哪个？",
        rewritten_query="比较机械键盘红轴和青轴，并说明办公室使用建议",
        question="比较红轴和青轴的手感、声音和办公适用性",
        produces="documents",
        capability="knowledge_search",
        tool_name="knowledge_search",
        call_id="knowledge-normal-1",
        artifact_value={"documents": [switch_document]},
        tool_output={"result_type": "documents", "documents": [switch_document]},
    )

    connection_items = [
        {"value": "有线 USB", "count": 120},
        {"value": "2.4G 无线", "count": 88},
        {"value": "蓝牙", "count": 76},
    ]
    connection_state = _single_usable_state(
        message="键盘一般有哪些连接方式？",
        rewritten_query="查询商城键盘品类包含的连接方式选项",
        question="查询键盘可选的连接方式",
        produces="facets",
        capability="catalog_facets",
        tool_name="catalog_facets",
        call_id="facets-normal-2",
        artifact_value={"facet": "spec_value", "items": connection_items},
        tool_output={
            "result_type": "facets",
            "facet": "spec_value",
            "items": connection_items,
        },
    )

    order_not_found_state = _single_unavailable_state(
        message="查一下订单 202607239999。",
        rewritten_query="查询当前用户订单 202607239999",
        question="查询订单 202607239999",
        produces="order",
        capability="order_lookup",
        tool_name="order_lookup",
        call_id="order-empty-1",
        outcome="not_found",
        reason="order_not_found",
        tool_output={"result_type": "not_found", "candidates": []},
    )

    historical_price_state = _single_unavailable_state(
        message="看看这款鼠标过去两年的价格变化。",
        rewritten_query="查询指定鼠标过去两年的历史价格变化",
        question="分析指定鼠标过去两年的历史价格趋势",
        produces="products",
        capability="catalog_search",
        tool_name="catalog_search",
        call_id="price-unsupported-1",
        outcome="unsupported",
        reason="query_not_supported_by_tool",
        tool_output={
            "result_type": "empty",
            "products": [],
            "diagnostics": [{"code": "unsupported_query"}],
        },
    )

    return [
        {
            "name": "normal_product_comparison_passes_guard",
            "state": comparison_state,
            "expected_action": "finish_answer",
            "checks": [
                (
                    "covers_both_products",
                    lambda text, _: "SwiftMouse" in text and "ErgoMouse Pro" in text,
                ),
                ("mentions_price", lambda text, _: "299" in text and "459" in text),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "normal_order_status_passes_guard",
            "state": order_state,
            "expected_action": "finish_answer",
            "checks": [
                ("states_shipping_status", lambda text, _: "已发货" in text),
                ("states_logistics", lambda text, _: "顺丰" in text or "运输中" in text),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "explicit_no_handoff_policy_question",
            "state": return_policy_state,
            "expected_action": "finish_answer",
            "checks": [
                (
                    "answers_return_conditions",
                    lambda text, _: _contains_any(text, ("七天", "7 天", "7天"))
                    and "包装" in text,
                ),
                (
                    "does_not_append_handoff_question",
                    lambda text, _: not text.endswith(LATE_HANDOFF_CONFIRMATION),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "manual_review_information_is_not_handoff",
            "state": review_policy_state,
            "expected_action": "finish_answer",
            "checks": [
                ("answers_review_process", lambda text, _: "审核" in text),
                (
                    "does_not_append_handoff_question",
                    lambda text, _: not text.endswith(LATE_HANDOFF_CONFIRMATION),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "normal_warranty_guidance_passes_guard",
            "state": warranty_state,
            "expected_action": "finish_answer",
            "checks": [
                ("answers_warranty_process", lambda text, _: "保修" in text),
                (
                    "mentions_required_material",
                    lambda text, _: _contains_any(text, ("故障说明", "购买凭证")),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "normal_switch_knowledge_passes_guard",
            "state": switch_state,
            "expected_action": "finish_answer",
            "checks": [
                ("compares_both_switches", lambda text, _: "红轴" in text and "青轴" in text),
                ("gives_office_guidance", lambda text, _: "办公" in text),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "normal_connection_facets_pass_guard",
            "state": connection_state,
            "expected_action": "finish_answer",
            "checks": [
                (
                    "lists_connection_values",
                    lambda text, _: all(item["value"] in text for item in connection_items),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "order_not_found_is_not_blocked",
            "state": order_not_found_state,
            "expected_action": "finish_answer",
            "checks": [
                (
                    "states_order_not_found",
                    lambda text, _: _contains_any(text, ("没有找到", "未找到")),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
        {
            "name": "non_handoff_unsupported_request_stays_unavailable",
            "state": historical_price_state,
            "expected_action": "finish_unavailable",
            "checks": [
                (
                    "explains_capability_limit",
                    lambda text, _: _contains_any(text, ("不支持", "无法", "不能")),
                ),
                (
                    "does_not_append_handoff_question",
                    lambda text, _: not text.endswith(LATE_HANDOFF_CONFIRMATION),
                ),
                ("plain_customer_language", _uses_plain_customer_language),
            ],
        },
    ]


async def _evaluate(case_name: str | None = None) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is not configured")
    runtime = AgentRuntime(cast(AsyncSession, None), settings)
    reports: list[dict[str, Any]] = []

    cases = [*_cases(), *_normal_guard_cases()]
    if case_name is not None:
        cases = [case for case in cases if case["name"] == case_name]
        if not cases:
            raise ValueError(f"unknown eval case: {case_name}")

    for case in cases:
        state = case["state"]
        context = build_answer_context(state)
        raw_decision = await runtime._invoke_orchestrator_decision(state, call_count=2)
        state["decision"] = raw_decision.model_dump(mode="json")
        guarded = await runtime._terminal_guard(state)
        finalized = await runtime._finalize_response(guarded)
        final_decision = finalized["decision"]
        final_answer = str(finalized["answer"])
        result_context = {
            "boundary": finalized["boundary"]["classification"],
            "suggested_actions": finalized["suggested_actions"],
        }
        checks = {
            "raw_control_action": (
                raw_decision.control_action == case["expected_action"]
            ),
            "final_control_action": (
                final_decision["control_action"] == case["expected_action"]
            ),
            "raw_handoff_offer": (
                raw_decision.offer_handoff_confirmation
                is case.get("expected_offer_handoff_confirmation", False)
            ),
            "final_handoff_offer": (
                final_decision["offer_handoff_confirmation"]
                is case.get("expected_offer_handoff_confirmation", False)
            ),
            **{
                name: check(final_answer, result_context)
                for name, check in case["checks"]
            },
        }
        reports.append(
            {
                "case": case["name"],
                "completion": context["completion"],
                "rewritten_query": context["rewritten_query"],
                "raw_control_action": raw_decision.control_action,
                "raw_answer": raw_decision.response,
                "raw_offer_handoff_confirmation": (
                    raw_decision.offer_handoff_confirmation
                ),
                "final_control_action": final_decision["control_action"],
                "final_offer_handoff_confirmation": final_decision[
                    "offer_handoff_confirmation"
                ],
                "terminal_guard_status": finalized.get("terminal_guard_status"),
                "answer": final_answer,
                "boundary": result_context["boundary"],
                "suggested_actions": result_context["suggested_actions"],
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    return reports


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="Run one named evaluation case.")
    args = parser.parse_args()
    reports = asyncio.run(_evaluate(args.case))
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    passed = sum(report["passed"] for report in reports)
    print(f"\nAnswer live eval: {passed}/{len(reports)} cases passed")
    raise SystemExit(0 if passed == len(reports) else 1)


if __name__ == "__main__":
    main()
