"""Shared deterministic interpretation of formal tool results."""

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel

from app.tools.catalog.outcomes import ResultReason, ResultStatus

CLARIFICATION_REASONS = {
    ResultReason.IDENTITY_CONFIRMATION_REQUIRED.value: "相似或有歧义候选中的完整商品名称",
    "order_confirmation_required": "确认放宽条件找到的候选是否为要找的订单",
    ResultReason.IDENTITY_MISMATCH.value: "相互一致的商品名称与编号",
    ResultReason.QUERY_NEEDS_CLARIFICATION.value: "明确的查询对象或范围",
    ResultReason.REQUIREMENT_NEEDS_CLARIFICATION.value: "将用途或主观要求具体化的必要参数",
    ResultReason.CONFLICTING_CONDITIONS.value: "需要保留或调整的冲突条件",
    "invalid_context_target": "可用的具体商品或型号",
    "insufficient_spu_ids": "至少两个明确的商品系列",
    "insufficient_active_series": "至少两个当前在售的商品系列",
}


OutcomeType = Literal[
    "usable",
    "empty",
    "not_found",
    "unsupported",
    "insufficient",
    "error",
]


class ToolOutcome(BaseModel):
    tool_call_id: str
    tool_name: str
    outcome: OutcomeType
    result_type: str | None = None
    has_usable_information: bool = False
    reason: str


def normalize_tool_result(result: Mapping[str, Any]) -> ToolOutcome:
    call_id = str(result.get("tool_call_id") or "")
    name = str(result.get("name") or "unknown_tool")
    execution = result.get("execution")
    if not isinstance(execution, Mapping) or not execution.get("ok"):
        code = _error_code(execution)
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=name,
            outcome="error",
            reason=f"tool_execution_failed:{code}",
        )

    output = execution.get("output")
    if not isinstance(output, Mapping):
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=name,
            outcome="error",
            reason="successful_execution_without_structured_output",
        )

    result_type = _optional_string(output.get("result_type"))
    if (
        name in {"catalog_search", "catalog_lookup"}
        and output.get("contract_version") in {"3", "4", "5"}
    ):
        status, reason = output.get("status"), str(output.get("reason_code") or "invalid_plan")
        if status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}:
            return _usable(call_id, name, result_type, reason)
        kind = {
            ResultStatus.NO_MATCH: "empty",
            ResultStatus.NOT_FOUND: "not_found",
            ResultStatus.CLARIFICATION_REQUIRED: "insufficient",
            ResultStatus.INVALID_QUERY: "insufficient",
            ResultStatus.UNSUPPORTED: "insufficient",
            ResultStatus.SPLIT_REQUIRED: "error",
            ResultStatus.PLANNER_ERROR: "error",
        }.get(status, "error")
        if status == ResultStatus.UNSUPPORTED and reason == ResultReason.REQUEST_NOT_SUPPORTED:
            kind = "unsupported"
        return ToolOutcome(tool_call_id=call_id, tool_name=name, result_type=result_type,
                           outcome=kind, reason=reason)

    # Facets/orders use diagnostics rather than the search status enum.
    for reason in (
        *CLARIFICATION_REASONS, "invalid_catalog_plan", "invalid_order_plan", "invalid_plan",
        "multiple_targets", "product_identity_required", "data_insufficient",
        "filter_data_unavailable",
        "target_inactive", "target_outside_scope",
        "request_not_supported",
    ):
        if _has_diagnostic(output, reason):
            if reason == "data_insufficient" and name == "catalog_facets" and output.get("items"):
                # Partial facets still carry facts; preserve missing-field limits in projection.
                continue
            kind = (
                "error" if reason in {
                    "invalid_catalog_plan", "invalid_order_plan", "invalid_plan",
                    "multiple_targets", "product_identity_required",
                } else "unsupported" if reason == "request_not_supported" else "insufficient"
            )
            return ToolOutcome(
                tool_call_id=call_id, tool_name=name, result_type=result_type,
                outcome=kind, reason=reason,
            )

    # A missing scenario mapping is a qualified catalog no-match, even though the
    # Tool plan retains generic unsupported markers for its own diagnostics.
    if name in {"catalog_search", "catalog_lookup"} and _has_diagnostic(
        output,
        "usage_mapping_unavailable",
    ):
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=name,
            outcome="empty",
            result_type=result_type,
            reason="usage_mapping_unavailable",
        )
    if name == "order_lookup" and result_type in {"planning_failed", "invalid_query"}:
        return ToolOutcome(
            tool_call_id=call_id, tool_name=name, result_type=result_type,
            outcome="error" if result_type == "planning_failed" else "insufficient",
            reason="invalid_order_plan" if result_type == "planning_failed"
            else "conflicting_conditions",
        )
    if _is_unsupported(name, output):
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=name,
            outcome="unsupported",
            result_type=result_type,
            reason="query_not_supported_by_tool",
        )

    if name in {"catalog_search", "catalog_lookup"}:
        products = output.get("products")
        return _collection_outcome(
            call_id,
            name,
            result_type,
            products,
            expected_result_type="products",
        )
    if name == "catalog_facets":
        if _has_diagnostic(output, "target_not_found"):
            return ToolOutcome(
                tool_call_id=call_id, tool_name=name, result_type=result_type,
                outcome="not_found", reason="target_not_found",
            )
        if not output.get("items") and output.get("field_issues"):
            return ToolOutcome(
                tool_call_id=call_id, tool_name=name, result_type=result_type,
                outcome="insufficient", reason="data_insufficient",
            )
        return _collection_outcome(
            call_id,
            name,
            result_type,
            output.get("items"),
            expected_result_type="facets",
        )
    if name == "order_lookup":
        if result_type == "single_order" and isinstance(output.get("order"), Mapping):
            return _usable(call_id, name, result_type, "order_found")
        if result_type == "order_count" and isinstance(output.get("total_match_count"), int):
            return _usable(call_id, name, result_type, "order_count_available")
        analysis_orders = output.get("analysis_orders")
        if (
            result_type == "order_analysis"
            and isinstance(analysis_orders, list)
            and analysis_orders
        ):
            return _usable(call_id, name, result_type, "order_analysis_available")
        candidates = output.get("candidates")
        if result_type == "order_candidates" and isinstance(candidates, list):
            if candidates:
                return _usable(call_id, name, result_type, "order_candidates_found")
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=name,
            outcome="not_found",
            result_type=result_type,
            reason="order_not_found",
        )
    if name in {"policy_search", "knowledge_search"}:
        if not output.get("documents"):
            return ToolOutcome(
                tool_call_id=call_id, tool_name=name, result_type=result_type,
                outcome="insufficient", reason="no_retrieval_evidence",
            )
        return _collection_outcome(
            call_id,
            name,
            result_type,
            output.get("documents"),
            expected_result_type="documents",
        )

    return ToolOutcome(
        tool_call_id=call_id,
        tool_name=name,
        outcome="error",
        result_type=result_type,
        reason="unknown_result_contract",
    )


def _collection_outcome(
    call_id: str,
    name: str,
    result_type: str | None,
    items: Any,
    *,
    expected_result_type: str,
) -> ToolOutcome:
    if isinstance(items, list) and items:
        if result_type == expected_result_type:
            return _usable(call_id, name, result_type, "non_empty_result_collection")
        return ToolOutcome(
            tool_call_id=call_id,
            tool_name=name,
            outcome="insufficient",
            result_type=result_type,
            reason="result_type_collection_mismatch",
        )
    return ToolOutcome(
        tool_call_id=call_id,
        tool_name=name,
        outcome="empty",
        result_type=result_type,
        reason="empty_result_collection",
    )


def _usable(call_id: str, name: str, result_type: str | None, reason: str) -> ToolOutcome:
    return ToolOutcome(
        tool_call_id=call_id,
        tool_name=name,
        outcome="usable",
        result_type=result_type,
        has_usable_information=True,
        reason=reason,
    )


def _is_unsupported(name: str, output: Mapping[str, Any]) -> bool:
    if _has_diagnostic(output, "unsupported_query"):
        return True
    if (
        name in {"catalog_search", "catalog_lookup"}
        and output.get("ranking_strategy") == "unsupported_query"
    ):
        return True
    query_plan = output.get("query_plan")
    if not isinstance(query_plan, Mapping):
        return False
    if query_plan.get("supported") is False:
        return True
    return False


def _has_diagnostic(output: Mapping[str, Any], code: str) -> bool:
    diagnostics = output.get("diagnostics")
    if not isinstance(diagnostics, list):
        return False
    return any(
        isinstance(diagnostic, Mapping) and diagnostic.get("code") == code
        for diagnostic in diagnostics
    )


def _error_code(execution: Any) -> str:
    if not isinstance(execution, Mapping):
        return "invalid_execution_result"
    error = execution.get("error")
    if isinstance(error, Mapping) and error.get("code"):
        return str(error["code"])
    return "execution_error"


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
