"""Single source of catalog-search outcome policy.

Pure functions and immutable definitions only: no database, LLM or schema imports.
The service selects a policy from query facts; the output schema validates against
that same policy. Domain actions are guidance, not orchestration performed here.
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Literal


class ResultStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    NO_MATCH = "no_match"
    NOT_FOUND = "not_found"
    CLARIFICATION_REQUIRED = "clarification_required"
    SPLIT_REQUIRED = "split_required"
    UNSUPPORTED = "unsupported"
    PLANNER_ERROR = "planner_error"
    INVALID_QUERY = "invalid_query"


class ResultReason(StrEnum):
    MATCHES_FOUND = "matches_found"
    ALTERNATIVES_FOUND = "alternatives_found"
    OBJECT_FACTS_RETURNED = "object_facts_returned"
    NO_MATCHING_SKU = "no_matching_sku"
    TARGET_NOT_FOUND = "target_not_found"
    QUERY_NEEDS_CLARIFICATION = "query_needs_clarification"
    REQUIREMENT_NEEDS_CLARIFICATION = "requirement_needs_clarification"
    MULTIPLE_TARGETS = "multiple_targets"
    REQUEST_NOT_SUPPORTED = "request_not_supported"
    DATA_INSUFFICIENT = "data_insufficient"
    FILTER_DATA_UNAVAILABLE = "filter_data_unavailable"
    TARGET_INACTIVE = "target_inactive"
    TARGET_OUTSIDE_SCOPE = "target_outside_scope"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_PLAN = "invalid_plan"
    CONFLICTING_CONDITIONS = "conflicting_conditions"


class ResultAction(StrEnum):
    USE_RESULT = "use_result"
    EXPLAIN_DIFFERENCES = "explain_differences"
    EXPLAIN_NO_MATCH = "explain_no_match"
    EXPLAIN_NOT_FOUND = "explain_not_found"
    CLARIFY_QUERY = "clarify_query"
    SPLIT_TASKS = "split_tasks"
    EXPLAIN_LIMITATION = "explain_limitation"
    RETRY_PLANNER = "retry_planner"
    EXPLAIN_INVALID_QUERY = "explain_invalid_query"


QueryState = Literal["not_executed", "completed"]
LookupStatus = Literal["found", "not_found", "inactive", "outside_scope", "identity_mismatch"]


@dataclass(frozen=True)
class ResultPolicy:
    status: ResultStatus
    reason: ResultReason
    action: ResultAction
    message: str
    query_states: tuple[QueryState, ...]
    retryable: bool = False

    def fields(self) -> dict[str, str | bool]:
        """Machine-facing fields; all producers use this, not separate mappings."""
        return {
            "status": self.status.value,
            "reason_code": self.reason.value,
            "recommended_action": self.action.value,
            "retryable": self.retryable,
        }

    def diagnostic(self, message: str | None = None) -> dict:
        return {
            "code": self.reason.value,
            "message": message or self.message,
            "recommended_action": self.action.value,
            "details": {
                "phase": (
                    "plan_validation"
                    if self.reason == ResultReason.INVALID_PLAN
                    else "query_result"
                    if self.query_states == ("completed",)
                    else "requirements"
                )
            },
            "severity": "warning"
            if self.status
            in {
                ResultStatus.CLARIFICATION_REQUIRED,
                ResultStatus.SPLIT_REQUIRED,
                ResultStatus.UNSUPPORTED,
                ResultStatus.PLANNER_ERROR,
                ResultStatus.INVALID_QUERY,
            }
            else "info",
        }


# One definition per reason. Adding a result is a deliberate policy change here,
# not an ad-hoc status/message/action branch in service or Pydantic validation.
_POLICIES = (
    ResultPolicy(
        ResultStatus.PARTIAL,
        ResultReason.ALTERNATIVES_FOUND,
        ResultAction.EXPLAIN_DIFFERENCES,
        "没有完全匹配；返回同品类、同身份/范围内最多差一个条件维度的替代候选，逐SKU查看condition_checks。",
        ("completed",),
    ),
    ResultPolicy(
        ResultStatus.PARTIAL,
        ResultReason.OBJECT_FACTS_RETURNED,
        ResultAction.EXPLAIN_DIFFERENCES,
        "返回指定对象的已记录资料；附加要求未完全满足或无法确认，不代表完全匹配或可替代。",
        ("completed",),
    ),
    ResultPolicy(
        ResultStatus.SUCCESS,
        ResultReason.MATCHES_FOUND,
        ResultAction.USE_RESULT,
        "查询成功；商品资料与型号汇总仅覆盖确认匹配的版本。",
        ("completed",),
    ),
    ResultPolicy(
        ResultStatus.NO_MATCH,
        ResultReason.NO_MATCHING_SKU,
        ResultAction.EXPLAIN_NO_MATCH,
        "已完成匹配检查，当前条件下没有匹配商品。",
        ("completed",),
    ),
    ResultPolicy(
        ResultStatus.NOT_FOUND,
        ResultReason.TARGET_NOT_FOUND,
        ResultAction.EXPLAIN_NOT_FOUND,
        "未找到指定商品对象。",
        ("completed",),
    ),
    ResultPolicy(
        ResultStatus.CLARIFICATION_REQUIRED,
        ResultReason.QUERY_NEEDS_CLARIFICATION,
        ResultAction.CLARIFY_QUERY,
        "请明确查询对象和有效条件。",
        ("not_executed",),
    ),
    ResultPolicy(
        ResultStatus.SPLIT_REQUIRED,
        ResultReason.MULTIPLE_TARGETS,
        ResultAction.SPLIT_TASKS,
        "一次只处理一个目标，请由外部拆分查询。",
        ("not_executed",),
    ),
    ResultPolicy(
        ResultStatus.CLARIFICATION_REQUIRED,
        ResultReason.REQUIREMENT_NEEDS_CLARIFICATION,
        ResultAction.CLARIFY_QUERY,
        "用途或主观要求尚未明确为可执行参数，请补充具体需求；未执行筛选。",
        ("not_executed",),
    ),
    ResultPolicy(
        ResultStatus.CLARIFICATION_REQUIRED,
        ResultReason.IDENTITY_MISMATCH,
        ResultAction.CLARIFY_QUERY,
        "指定编号存在，但名称与该对象不一致；请确认商品名称或编号，未改查其他商品。",
        ("completed",),
    ),
    ResultPolicy(
        ResultStatus.UNSUPPORTED,
        ResultReason.REQUEST_NOT_SUPPORTED,
        ResultAction.EXPLAIN_LIMITATION,
        "当前工具能力或查询规则不支持该请求。",
        ("not_executed",),
    ),
    ResultPolicy(
        ResultStatus.UNSUPPORTED,
        ResultReason.FILTER_DATA_UNAVAILABLE,
        ResultAction.EXPLAIN_LIMITATION,
        "当前字段资料体系无法承接该筛选要求；未执行查询，不代表商品不支持。",
        ("not_executed",),
    ),
    ResultPolicy(
        ResultStatus.UNSUPPORTED,
        ResultReason.DATA_INSUFFICIENT,
        ResultAction.EXPLAIN_LIMITATION,
        "当前商品资料不足以完成筛选，不能据此判定商品不支持或没有符合条件的商品。",
        ("not_executed", "completed"),  # Keep historical v3 results readable.
    ),
    ResultPolicy(
        ResultStatus.UNSUPPORTED,
        ResultReason.TARGET_INACTIVE,
        ResultAction.EXPLAIN_LIMITATION,
        "指定商品存在，但当前状态不可用于查询。",
        ("completed",),
    ),
    ResultPolicy(
        ResultStatus.UNSUPPORTED,
        ResultReason.TARGET_OUTSIDE_SCOPE,
        ResultAction.EXPLAIN_LIMITATION,
        "指定商品存在，但不在本次限定范围内。",
        ("completed",),
    ),
    ResultPolicy(
        ResultStatus.INVALID_QUERY,
        ResultReason.CONFLICTING_CONDITIONS,
        ResultAction.EXPLAIN_INVALID_QUERY,
        "查询条件无法同时成立；请根据冲突字段与条件解释原因。",
        ("not_executed",),
    ),
    ResultPolicy(
        ResultStatus.PLANNER_ERROR,
        ResultReason.INVALID_PLAN,
        ResultAction.RETRY_PLANNER,
        "未得到有效查询计划，未执行放宽条件的搜索。",
        ("not_executed",),
        retryable=True,
    ),
)
POLICIES = MappingProxyType({rule.reason: rule for rule in _POLICIES})
# First policy for each status is its default; the ordering is explicit above.
DEFAULT_REASONS = MappingProxyType(
    {
        status: next(rule.reason for rule in _POLICIES if rule.status == status)
        for status in ResultStatus
    }
)


@dataclass(frozen=True)
class QueryIssue:
    reason: ResultReason
    message: str
    details: dict = field(default_factory=dict)


def policy_for(reason: ResultReason | str) -> ResultPolicy:
    return POLICIES[ResultReason(reason)]


def default_reason(status: ResultStatus | str) -> ResultReason:
    return DEFAULT_REASONS[ResultStatus(status)]


def completed_policy(
    *,
    total_matches: int,
    returned_count: int,
    unknown_count: int,
    object_status: LookupStatus | None = None,
    named_target_missing: bool = False,
    alternative_count: int = 0,
    object_fact_count: int = 0,
) -> ResultPolicy:
    """Classify completed query facts, independently of product quality and pagination."""
    if min(total_matches, returned_count, unknown_count) < 0 or returned_count > total_matches:
        raise ValueError("invalid query counts")
    if total_matches:
        policy = policy_for(ResultReason.MATCHES_FOUND)
        if not returned_count:
            return replace(policy, message="查询成功，存在匹配商品，但当前页为空；请调整 offset。")
        return policy
    if alternative_count:
        if unknown_count:
            raise ValueError("unknown strict matches cannot trigger alternatives")
        return policy_for(ResultReason.ALTERNATIVES_FOUND)
    if object_fact_count:
        return policy_for(ResultReason.OBJECT_FACTS_RETURNED)
    if object_status == "not_found" or named_target_missing:
        return policy_for(ResultReason.TARGET_NOT_FOUND)
    if object_status == "inactive":
        return policy_for(ResultReason.TARGET_INACTIVE)
    if object_status == "outside_scope":
        return policy_for(ResultReason.TARGET_OUTSIDE_SCOPE)
    if object_status == "identity_mismatch":
        return policy_for(ResultReason.IDENTITY_MISMATCH)
    if unknown_count:
        return policy_for(ResultReason.DATA_INSUFFICIENT)
    return policy_for(ResultReason.NO_MATCHING_SKU)


def validate_policy_fields(
    *,
    status: ResultStatus,
    reason: ResultReason,
    action: ResultAction,
    retryable: bool,
    query_state: QueryState,
) -> None:
    policy = policy_for(reason)
    if (status, action, retryable) != (policy.status, policy.action, policy.retryable):
        raise ValueError("status, reason, action and retry policy must agree")
    if query_state not in policy.query_states:
        raise ValueError("result policy is not valid in this query phase")


def data_quality(conflicted: int, missing: int) -> Literal["clear", "issues"]:
    return "issues" if conflicted or missing else "clear"


def fact_diagnostics(
    conflicted: int, missing: int, unknown: int, *, action: ResultAction
) -> list[dict[str, str]]:
    diagnostics = []
    if conflicted or missing:
        diagnostics.append(
            {
                "code": "product_fact_issues",
                "severity": "warning",
                "message": "匹配商品含缺失或冲突资料；使用已知事实并保留 field_issues 提示。",
                "recommended_action": action.value,
            }
        )
    if unknown:
        diagnostics.append(
            {
                "code": "uncertain_matches",
                "severity": "warning",
                "message": f"有 {unknown} 个版本无法确认是否匹配；不要将资料未知当作明确不支持。",
                "recommended_action": action.value,
            }
        )
    return diagnostics
