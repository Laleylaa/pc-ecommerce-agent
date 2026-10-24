"""Deprecated dotted-name facade for legacy clients; remove with legacy runtime.

No registration API or independent handler map. All calls use contracts.py validation.
"""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.tools.catalog.facets_planner import FacetPlanner
from app.tools.contracts import ToolCatalog, _error_result, build_tool_catalog, execute_bound_tool
from app.tools.orders import OrderQueryPlanner
from app.tools.schemas import ToolExecutionResult

ToolRegistryError = ValueError


class ToolRegistry:
    def __init__(self, catalog: ToolCatalog):
        self.catalog = catalog

    @property
    def tool_names(self) -> list[str]:
        return sorted(c.registry_name for c in self.catalog.list_contracts())

    async def execute(self, name: str, input_data: dict[str, Any]) -> ToolExecutionResult:
        bound = self.catalog.get_by_registry_name(name)
        if bound is None:
            return _error_result(name, "unknown_tool")
        # This legacy entry accepts trusted internal input, never model arguments.
        arguments = dict(input_data)
        runtime = {"user_id": 0}
        for field in bound.contract.runtime_fields:
            if field in arguments:
                runtime[field] = arguments.pop(field)
        result = await execute_bound_tool(bound, arguments, runtime)
        return result.model_copy(update={"tool_name": name})


def build_tool_registry(
    session: AsyncSession,
    catalog_planner: FacetPlanner | None = None,
    order_planner: OrderQueryPlanner | None = None,
    settings: Settings | None = None,
    catalog_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ToolRegistry:
    from app.core.config import get_settings

    settings = settings or get_settings()
    tool_catalog = build_tool_catalog(
        session,
        settings=settings,
        catalog_planner=catalog_planner,
        order_planner=order_planner,
        catalog_session_factory=catalog_session_factory,
    )
    return ToolRegistry(tool_catalog)
