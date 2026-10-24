"""Facet service: plan, validate, aggregate. No candidate-search orchestration."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.catalog_search import CatalogSearchRepository
from app.tools.catalog.facets_plan import FacetQueryPlan, _validate_catalog_facet_plan
from app.tools.catalog.facets_planner import CatalogFacetPlanner, FacetPlanner
from app.tools.schemas import CatalogFacetInput, CatalogFacetOutput


class CatalogFacetService:
    def __init__(self, session: AsyncSession, planner: FacetPlanner | None = None):
        self.session = session
        if planner is None:
            # Preserve the existing explicitly disabled-LLM / offline fixture path.
            from app.tools.catalog.compatibility import RuleBasedCatalogQueryPlanner
            planner = RuleBasedCatalogQueryPlanner()
        self.planner = planner

    async def facets(self, request: CatalogFacetInput) -> CatalogFacetOutput:
        from app.tools.catalog.facets_execution import execute_facets

        plan = await self._safe_plan_facets(request)
        return await execute_facets(CatalogSearchRepository(self.session), request, plan)


    async def _safe_plan_facets(self, request: CatalogFacetInput) -> FacetQueryPlan:
        # Failed parsing must reach the formal execution error boundary. Replacing
        # it with a different query cannot prove that the caller's constraints survive.
        from app.tools.catalog.facets_execution import explicit_spec_request

        if direct := explicit_spec_request(request):
            plan = FacetQueryPlan(
                **direct, query=request.query, limit=request.limit,
                filters=request.filters, min_price=request.min_price, max_price=request.max_price,
                category=request.category, brand=request.brand,
            )
        elif isinstance(self.planner, CatalogFacetPlanner):
            plan = await self.planner.plan_facets_with_repository(
                request, CatalogSearchRepository(self.session),
            )
        else:
            plan = await self.planner.plan_facets(request)
        return _validate_catalog_facet_plan(plan, request=request)
