"""Pure feedback formatting for explicitly classified model-output validation errors."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError


class OutputErrorType(StrEnum):
    FORMAT_ERROR = "FORMAT_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    RULE_ERROR = "RULE_ERROR"


_REPAIR_INSTRUCTIONS = {
    OutputErrorType.FORMAT_ERROR: (
        "按指定的响应形式重新提交完整输出，修正解析或工具调用格式错误。"
    ),
    OutputErrorType.SCHEMA_ERROR: (
        "按给定 schema 和错误路径修正结构、必填字段或字段值，重新提交完整输出。"
    ),
    OutputErrorType.RULE_ERROR: (
        "按错误原因修正字段或任务关系，保留原请求的目标与条件，重新提交完整输出。"
    ),
}


@dataclass(frozen=True)
class ValidationIssue:
    error_type: OutputErrorType
    message: str
    path: tuple[str | int, ...] = ()
    # Native parser/schema codes are diagnostic details, not extra feedback categories.
    code: str = "invalid_plan"


class OutputValidationError(ValueError):
    """An expected output violation; ordinary program/runtime errors are not classified."""

    def __init__(
        self,
        message: str,
        *,
        error_type: OutputErrorType = OutputErrorType.RULE_ERROR,
        path: tuple[str | int, ...] = (),
        code: str = "invalid_plan",
        issues: Sequence[ValidationIssue] | None = None,
    ):
        super().__init__(message)
        self.issue = ValidationIssue(error_type, message, path, code)
        self.issues = tuple(issues) if issues else (self.issue,)


def build_feedback(issues: Sequence[ValidationIssue]) -> dict:
    """Map three categories to instructions without querying or interpreting business data."""
    return {
        "errors": [
            {
                "type": issue.error_type.value,
                "path": list(issue.path),
                "code": issue.code,
                "message": issue.message,
                "repair_instruction": _REPAIR_INSTRUCTIONS[issue.error_type],
            }
            for issue in issues
        ]
    }


def validation_feedback(exc: ValidationError | OutputValidationError) -> dict:
    """Adapt schema diagnostics and explicitly marked rules; never guess from error text."""
    if isinstance(exc, OutputValidationError):
        return build_feedback(exc.issues)
    if not isinstance(exc, ValidationError):
        raise TypeError("feedback requires an explicitly classified output validation error")
    issues = []
    for error in exc.errors(include_input=False, include_url=False):
        cause = error.get("ctx", {}).get("error")
        if isinstance(cause, OutputValidationError):
            # Pydantic wraps model/field validators in a ValidationError. Keep their category.
            issues.extend(
                ValidationIssue(i.error_type, i.message, (*error["loc"], *i.path), i.code)
                for i in cause.issues
            )
            continue
        else:
            error_type = (
                OutputErrorType.FORMAT_ERROR
                if error["type"] == "json_invalid"
                else OutputErrorType.SCHEMA_ERROR
            )
            path, message = tuple(error["loc"]), error["msg"]
        issues.append(ValidationIssue(error_type, message, path, error["type"]))
    return build_feedback(issues)
