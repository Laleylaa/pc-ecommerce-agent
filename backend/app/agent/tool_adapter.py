"""P1 domain-tool admission/execution. Does not run or replace the chat runtime."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.agent.evidence import EvidenceRecord, EvidenceStore, ToolInvocation
from app.tools.contracts import ToolCatalog, ToolRuntimeContext, execute_bound_tool
from app.tools.schemas import CatalogToolRuntimeContext, ToolError, ToolExecutionResult


class AdmissionError(ValueError):
    """Stable rejection code; do not echo trusted context or exception payloads."""


@dataclass(frozen=True)
class QueryOperation:
    operation_id: str
    goal_id: str
    tool_name: str
    arguments: dict[str, Any]


# P2 supplies the registered-goal/prerequisite check, called again before each attempt.
GoalAdmission = Callable[[str], None]
# P2 supplies admission and an absolute deadline in the running event loop's clock.
AttemptAdmission = Callable[[QueryOperation], float | None]


class QueryToolAdapter:
    """Request-owned, serial adapter over the existing domain executor.

    prepare() and SDK binding are trusted runtime APIs, never model tools. The
    mandatory goal guard must reject unknown, blocked or non-query goals; P1 does
    not infer goals or evaluate natural-language conditions. No implicit retries.
    """

    def __init__(
        self, catalog: ToolCatalog, runtime: ToolRuntimeContext, *,
        admit_goal: GoalAdmission, before_attempt: AttemptAdmission | None = None,
        attempt_scope: Callable[[QueryOperation], AbstractContextManager] | None = None,
    ) -> None:
        self.catalog = catalog
        self._runtime = runtime.model_copy(deep=True)
        self._admit_goal = admit_goal
        self._before_attempt = before_attempt
        self._attempt_scope = attempt_scope
        self._contexts: dict[str, CatalogToolRuntimeContext] = {}
        self.evidence = EvidenceStore()
        self._operations: dict[str, QueryOperation] = {}
        self._attempts: dict[str, list[EvidenceRecord]] = {}
        self._lock = asyncio.Lock()

    def prepare(
        self, goal_id: str, tool_name: str, arguments: dict[str, Any], *,
        catalog_context: CatalogToolRuntimeContext | None = None,
    ) -> str:
        self._admit_goal(goal_id)
        bound = self.catalog.get_by_llm_name(tool_name)
        if bound is None or not bound.contract.read_only:
            raise AdmissionError("unknown_or_non_query_tool")
        if bound.contract.requires_auth and self._runtime.user_id <= 0:
            raise AdmissionError("authentication_required")
        try:
            public = bound.contract.public_input_model.model_validate(arguments, strict=True)
        except ValidationError as exc:
            raise AdmissionError("invalid_input") from exc
        operation = QueryOperation(
            operation_id=f"op_{uuid4().hex}", goal_id=goal_id, tool_name=tool_name,
            arguments=public.model_dump(mode="json", exclude_none=True),
        )
        self._operations[operation.operation_id] = operation
        self._attempts[operation.operation_id] = []
        if catalog_context is not None:
            self._contexts[operation.operation_id] = catalog_context.model_copy(deep=True)
        return operation.operation_id

    def operation(self, operation_id: str) -> QueryOperation:
        if operation_id not in self._operations:
            raise AdmissionError("unknown_operation")
        return deepcopy(self._operations[operation_id])

    def discard_prepared(self, operation_id: str) -> None:
        """Drop an unexecuted proposal; actual attempts/evidence must never be erased."""
        self.operation(operation_id)
        if self._attempts[operation_id]:
            raise AdmissionError("cannot_discard_attempted_operation")
        del self._operations[operation_id]
        del self._attempts[operation_id]
        self._contexts.pop(operation_id, None)

    @contextmanager
    def preparing_batch(self) -> Iterator[None]:
        """Roll back proposals if synchronous, pre-execution batch admission fails."""
        previous = set(self._operations)
        try:
            yield
        except Exception:
            for operation_id in self._operations.keys() - previous:
                self.discard_prepared(operation_id)
            raise

    async def execute(self, operation_id: str) -> EvidenceRecord:
        async with self._lock:
            operation = self.operation(operation_id)
            self._admit_goal(operation.goal_id)
            attempts = self._attempts[operation_id]
            if attempts:
                last = attempts[-1].result
                if len(attempts) >= 2 or last.ok or not last.error or not last.error.retryable:
                    raise AdmissionError("retry_not_allowed")
            deadline = (
                self._before_attempt(deepcopy(operation)) if self._before_attempt else None
            )
            if deadline is not None and deadline <= asyncio.get_running_loop().time():
                raise AdmissionError("deadline_exhausted")
            bound = self.catalog.get_by_llm_name(operation.tool_name)
            assert bound is not None
            started_at, started = datetime.now(UTC), monotonic()
            cancelled = False
            runtime = self._runtime.model_dump(mode="python")
            if operation_id in self._contexts:
                runtime["catalog_context"] = self._contexts[operation_id].model_dump(mode="python")
            scope = self._attempt_scope(operation) if self._attempt_scope else nullcontext()
            try:
                with scope:
                    async with asyncio.timeout_at(deadline):
                        result = await execute_bound_tool(
                            bound, deepcopy(operation.arguments), runtime,
                        )
            except TimeoutError:
                result = _failure(operation.tool_name, "deadline_exhausted")
            except asyncio.CancelledError:
                cancelled = True
                result = _failure(operation.tool_name, "cancelled")
            record = self.evidence.record(
                ToolInvocation(
                    invocation_id=f"attempt_{uuid4().hex}",
                    operation_id=operation.operation_id, goal_id=operation.goal_id,
                    tool_name=operation.tool_name, arguments=deepcopy(operation.arguments),
                    attempt=len(attempts) + 1, started_at=started_at,
                    elapsed_seconds=monotonic() - started,
                    status="cancelled" if cancelled else "succeeded" if result.ok else "failed",
                ),
                result, datetime.now(UTC),
            )
            attempts.append(record.model_copy(deep=True))
            if cancelled:
                raise asyncio.CancelledError
            return record

    def attempts(self, operation_id: str) -> tuple[EvidenceRecord, ...]:
        self.operation(operation_id)
        return tuple(record.model_copy(deep=True) for record in self._attempts[operation_id])


def _failure(tool_name: str, code: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name=tool_name, ok=False,
        error=ToolError(code=code, message=code, recommended_action="stop"),
    )
