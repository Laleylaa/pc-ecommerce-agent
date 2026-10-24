"""Single-goal search: validated plan -> SKU facts -> deterministic result window."""

from typing import Literal

from pydantic import ValidationError

from app.core.llm_feedback import OutputValidationError, validation_feedback
from app.core.loop_tracing import program_event, tracing_active
from app.repositories.catalog_search import CatalogSearchRepository
from app.schemas.catalog_search import (
    CatalogSearchRequest,
    CatalogSearchResult,
    SearchDiagnostic,
    SearchPlan,
)
from app.tools.catalog_search.engine import CatalogSearchEngine, UnsupportedCatalogFilter
from app.tools.catalog_search.identity import within_scope as within_scope
from app.tools.catalog_search.outcomes import ResultReason, policy_for
from app.tools.catalog_search.planner import CatalogSearchPlanner, SearchPlanner, explicit_lookup
from app.tools.catalog_search.presentation import build_result
from app.tools.catalog_search.validation import validate_plan


class CatalogSearchService:
    def __init__(
        self, repository: CatalogSearchRepository, planner: SearchPlanner | None = None,
        *, operation: Literal["search", "lookup"] = "search",
    ):
        self.engine = CatalogSearchEngine(repository)
        self.operation = operation
        self.planner = planner or CatalogSearchPlanner(operation=operation)

    async def search(self, request: CatalogSearchRequest) -> CatalogSearchResult:
        if tracing_active():
            program_event("商品查询上下文", outcome="prepared",
                          scope=request.scope.model_dump(mode="json"),
                          scope_source="tool_request", category_hint=request.category_hint,
                          hint_usage="prompt_context_only", offset=request.offset,
                          limit=request.limit)
        try:
            direct = explicit_lookup(request.query) if self.operation == "lookup" else None
            plan = direct or await self.planner.plan(request)
            plan = SearchPlan.model_validate(plan)
        except (ValidationError, OutputValidationError) as exc:
            return self._stop(
                ResultReason.INVALID_PLAN,
                details={"validation_feedback": validation_feedback(exc)},
            )
        planner_name = "explicit_ids" if direct else type(self.planner).__name__
        if plan.operation != self.operation:
            return self._stop(ResultReason.INVALID_PLAN, "计划操作与正式工具职责不符。", plan=plan)
        issue = validate_plan(plan, request)
        if issue:
            return self._stop(
                issue.reason, issue.message, plan=plan, planner=planner_name, details=issue.details
            )
        if tracing_active():
            program_event("执行计划确认", outcome="accepted", planner=planner_name,
                          route="explicit_reference" if direct else "planner",
                          plan=plan.model_dump(mode="json"))
        try:
            execution = await self.engine.execute(request, plan)
        except UnsupportedCatalogFilter as exc:
            return self._stop(
                ResultReason.FILTER_DATA_UNAVAILABLE, str(exc), plan=plan, planner=planner_name,
                details={"phase": "capability_after_identity", "category": exc.category,
                         "field": exc.field},
            )
        except ValueError as exc:
            if str(exc) != "catalog_scope_too_large":
                raise
            return self._stop(
                ResultReason.REQUEST_NOT_SUPPORTED, "查询范围过大，请缩小范围。",
                plan=plan, planner=planner_name,
            )
        return build_result(request, plan, execution, planner_name)

    @staticmethod
    def _stop(
        reason: ResultReason,
        message: str | None = None,
        *,
        plan: SearchPlan | None = None,
        planner: str = "",
        details: dict | None = None,
    ) -> CatalogSearchResult:
        program_event("商品查询提前结束", outcome="not_executed", reason=reason.value,
                      message=message, details=details or {}, planner=planner)
        policy = policy_for(reason)
        diagnostic = policy.diagnostic(message)
        diagnostic["details"].update(details or {})
        return CatalogSearchResult(
            result_type="empty",
            **policy.fields(),
            query_state="not_executed",
            query_plan=plan.model_dump(mode="json") if plan else {},
            planner=planner,
            result_purpose=plan.purpose if plan else "search",
            diagnostics=[SearchDiagnostic(**diagnostic)],
        )
