"""Shared orchestration budgets."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class PlanningCallPolicy:
    """LLM1/LLM2 share a deadline; each stage owns its retry counters."""

    call_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 90.0
    max_transport_retries: int = 1
    max_output_repairs: int = 1
    max_attempts: int = 3  # Includes the first call, transport retries and repairs.
    retry_delay_seconds: float = 1.0
    sdk_max_retries: int = 0

    def __post_init__(self) -> None:
        for name in ("call_timeout_seconds", "total_timeout_seconds"):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be finite and nonnegative")
        for name in ("max_transport_retries", "max_output_repairs", "max_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == "max_attempts" else 0):
                raise ValueError(f"{name} has an invalid attempt count")
        if self.sdk_max_retries != 0:
            raise ValueError("Planning owns retries; SDK retries must remain disabled")


PLANNING_CALL_POLICY = PlanningCallPolicy()

# Same bounded transport/repair policy; answers own a separate phase deadline.
ANSWER_CALL_POLICY = PlanningCallPolicy()
TOOL_CONCURRENCY_LIMIT = 3
TURN_TIMEOUT_SECONDS = 240.0
CLEANUP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ToolRetryPolicy:
    """Timeout attempts stay inside a wave; they do not consume DAG depth."""

    max_timeout_retries: int = 1

    def __post_init__(self) -> None:
        if type(self.max_timeout_retries) is not int or self.max_timeout_retries not in (0, 1):
            raise ValueError("retry_once permits zero or one timeout retry")

    @property
    def max_attempts(self) -> int:
        return 1 + self.max_timeout_retries

    def allows_retry(
        self, *, completed_attempts: int, code: str, retryable: bool, action: str | None
    ) -> bool:
        return (
            completed_attempts < self.max_attempts
            and code == "timeout"
            and retryable
            and action == "retry_once"
        )


TOOL_RETRY_POLICY = ToolRetryPolicy()

MAX_ORCHESTRATOR_CALLS = 3
MAX_TOOL_WAVES = 2
# Compatibility export; planning callers read the policy, not a second fixed budget.
MAX_REQUEST_ROUTER_CALLS = PLANNING_CALL_POLICY.max_attempts

__all__ = [
    "MAX_ORCHESTRATOR_CALLS",
    "MAX_REQUEST_ROUTER_CALLS",
    "MAX_TOOL_WAVES",
    "PLANNING_CALL_POLICY",
    "PlanningCallPolicy",
    "TOOL_RETRY_POLICY",
    "ToolRetryPolicy",
]
