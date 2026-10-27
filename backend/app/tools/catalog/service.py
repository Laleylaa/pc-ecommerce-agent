"""Single-goal search: validated plan -> SKU facts -> deterministic result window."""

from typing import Literal

from pydantic import ValidationError

from app.core.llm_feedback import OutputValidationError, validation_feedback
from app.repositories.catalog_search import CatalogSearchRepository
from app.schemas.catalog_search import (
    CatalogSearchRequest,
    CatalogSearchResult,
    SearchDiagnostic,
    SearchPlan,
)
from app.tools.catalog.engine import CatalogSearchEngine
from app.tools.catalog.identity import within_scope as within_scope
from app.tools.catalog.outcomes import ResultReason, policy_for
from app.tools.catalog.planner import CatalogSearchPlanner, SearchPlanner, explicit_lookup
from app.tools.catalog.presentation import build_result
from app.tools.catalog.validation import validate_plan


class CatalogSearchService:
    def __init__(
        self, repository: CatalogSearchRepository, planner: SearchPlanner | None = None,
        *, operation: Literal["search", "lookup"] = "search",
    ):
        self.engine = CatalogSearchEngine(repository)
        self.operation = operation
        self.planner = planner or CatalogSearchPlanner(operation=operation)

    async def search(self, request: CatalogSearchRequest) -> CatalogSearchResult:
        prepared = None
        try:
            direct = explicit_lookup(request.query) if self.operation == "lookup" else None
            if direct is not None:
                plan = direct
            elif isinstance(self.planner, CatalogSearchPlanner):
                plan, prepared = await self.planner.plan_with_repository(
                    request, self.engine.repository,
                )
            else:
                plan = await self.planner.plan(request)
            plan = SearchPlan.model_validate(plan)
            plan.requested_limit = request.requested_count or request.limit
        except (ValidationError, OutputValidationError) as exc:
            return self._stop(
                ResultReason.INVALID_PLAN,
                details={"validation_feedback": validation_feedback(exc)},
            )
        except ValueError as exc:
            if str(exc) != "catalog_scope_too_large":
                raise
            return self._stop(ResultReason.REQUEST_NOT_SUPPORTED, "查询范围过大，请缩小范围。")
        planner_name = "explicit_ids" if direct else type(self.planner).__name__
        if plan.operation != self.operation:
            return self._stop(ResultReason.INVALID_PLAN, "计划操作与正式工具职责不符。", plan=plan)
        issue = validate_plan(
            plan, request, resolved_categories=prepared.categories if prepared else (),
        )
        if issue:
            return self._stop(
                issue.reason, issue.message, plan=plan, planner=planner_name,
                details=issue.details | (
                    {"resolved_scope": prepared.payload()} if prepared else {}
                )
            )
        try:
            execution = await self.engine.execute(
                request, plan, identity=prepared.identity if prepared else None,
            )
        except ValueError as exc:
            if str(exc) != "catalog_scope_too_large":
                raise
            return self._stop(
                ResultReason.REQUEST_NOT_SUPPORTED, "查询范围过大，请缩小范围。",
                plan=plan, planner=planner_name,
            )
        result = build_result(request, plan, execution, planner_name)
        if prepared is not None:
            result.query_plan["scope_resolution"] = prepared.payload()
            if prepared.identity is not None and not prepared.identity.records:
                result.query_plan["requirements_pending"] = request.query
        return result

    @staticmethod
    def _stop(
        reason: ResultReason,
        message: str | None = None,
        *,
        plan: SearchPlan | None = None,
        planner: str = "",
        details: dict | None = None,
    ) -> CatalogSearchResult:
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
