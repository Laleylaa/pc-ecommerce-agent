"""Public failure vocabulary and deterministic policies; no runtime dependencies."""

from enum import StrEnum
from types import MappingProxyType


class ToolErrorCode(StrEnum):
    INVALID_ARGUMENTS = "invalid_arguments"
    ACCESS_DENIED = "access_denied"
    RESOURCE_LIMIT = "resource_limit"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    PLANNING_FAILED = "planning_failed"
    INTERNAL_ERROR = "internal_error"


class ToolNextAction(StrEnum):
    REVISE_ARGUMENTS = "revise_arguments"
    REQUEST_AUTHENTICATION = "request_authentication"
    NARROW_REQUEST = "narrow_request"
    RETRY = "retry"
    STOP = "stop"


ALLOWED_ERROR_ACTIONS = MappingProxyType({
    ToolErrorCode.INVALID_ARGUMENTS: frozenset({ToolNextAction.REVISE_ARGUMENTS}),
    ToolErrorCode.ACCESS_DENIED: frozenset({
        ToolNextAction.REQUEST_AUTHENTICATION, ToolNextAction.STOP,
    }),
    ToolErrorCode.RESOURCE_LIMIT: frozenset({ToolNextAction.NARROW_REQUEST, ToolNextAction.STOP}),
    ToolErrorCode.DEPENDENCY_UNAVAILABLE: frozenset({ToolNextAction.RETRY, ToolNextAction.STOP}),
    ToolErrorCode.PLANNING_FAILED: frozenset({ToolNextAction.STOP}),
    ToolErrorCode.INTERNAL_ERROR: frozenset({ToolNextAction.STOP}),
})

# Internal identifiers are diagnostic provenance, never the public vocabulary.
_POLICIES = MappingProxyType({
    "invalid_input": (
        "invalid_arguments", "revise_arguments", "调用参数不合法，请按参数说明修正。",
    ),
    "unauthorized": ("access_denied", "request_authentication", "请先登录后再查询。"),
    "forbidden": ("access_denied", "stop", "当前调用没有访问权限。"),
    "timeout": ("dependency_unavailable", "retry", "工具查询超时，本次未取得可用结果。"),
    "dependency_unavailable": (
        "dependency_unavailable", "stop", "查询依赖当前不可用，本次未取得可用结果。",
    ),
    "retrieval_busy": ("dependency_unavailable", "stop", "检索服务繁忙，本次未取得可用结果。"),
    "planning_output_invalid": (
        "planning_failed", "stop", "未能形成可靠查询计划，本次未取得可用结果。",
    ),
    "planning_review_inconclusive": (
        "planning_failed", "stop", "未能确认查询计划可靠，本次未取得可用结果。",
    ),
    "planning_model_rejected": (
        "dependency_unavailable", "stop", "规划服务无法完成请求，本次未取得可用结果。",
    ),
    "planning_transport_failed": (
        "dependency_unavailable", "stop", "规划服务连接失败，内部重试已结束。",
    ),
    "planning_dependency_timeout": (
        "dependency_unavailable", "stop", "规划依赖服务超时，内部重试已结束。",
    ),
    "planning_call_timeout": (
        "resource_limit", "stop", "规划执行时间已耗尽，本次未取得可用结果。",
    ),
    "output_budget_exceeded": (
        "resource_limit", "narrow_request", "结果无法在输出预算内完整交付，请缩小展示或拆分请求。",
    ),
    "observation_budget_exceeded": (
        "resource_limit", "narrow_request",
        "工具材料超过交接预算，未交付事实，请缩小展示或拆分请求。",
    ),
    "deadline_exhausted": ("resource_limit", "stop", "本轮执行时间已耗尽，停止继续查询。"),
    "cancelled": ("resource_limit", "stop", "本次查询已取消。"),
    "model_budget_exhausted": ("resource_limit", "stop", "本轮模型调用预算已耗尽。"),
    "recovery_budget_exhausted": ("resource_limit", "stop", "恢复执行预算已耗尽。"),
    "recovery_repeated_call": ("resource_limit", "stop", "重复恢复调用未执行。"),
})

# Web calls may already have spent credits. No public policy authorizes replay.
_WEB_POLICIES = MappingProxyType({
    "web_search_not_configured": "网页检索服务尚不可用。",
    "web_search_timeout": "网页检索超时，本次未取得可用结果，不自动重试。",
    "web_search_unavailable": "网页检索服务当前不可用。",
    "web_search_auth_failed": "网页检索服务配置不可用，本次未取得可用结果。",
    "web_search_quota_exceeded": "网页检索服务额度或速率受限，本次不继续调用。",
    "web_search_invalid_response": "网页检索服务未返回可靠结果。",
})


def error_payload(reason: str) -> dict[str, str]:
    """Never publish exception text or infer recovery from a message substring."""
    if reason in _WEB_POLICIES:
        code, action, message = "dependency_unavailable", "stop", _WEB_POLICIES[reason]
    else:
        code, action, message = _POLICIES.get(reason, (
            "internal_error", "stop", "工具内部处理失败，本次未取得可用结果。",
        ))
    return {"code": code, "message": message, "next_action": action}
