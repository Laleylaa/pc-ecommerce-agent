from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.tools.catalog.facets_planner import FacetPlanner
from app.tools.contracts import build_tool_catalog
from app.tools.orders import OrderQueryPlanner
from app.tools.schemas import ToolError, ToolExecutionResult


class ToolRegistryError(ValueError):
    pass


ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]


class RegisteredTool:
    def __init__(
        self,
        name: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
    ):
        self.name = name
        self.input_model = input_model
        self.handler = handler


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
    ) -> None:
        if name in self._tools:
            raise ToolRegistryError(f"tool already registered: {name}")
        self._tools[name] = RegisteredTool(name, input_model, handler)

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    @property
    def registered_tools(self) -> dict[str, RegisteredTool]:
        return dict(self._tools)

    async def execute(self, name: str, input_data: dict[str, Any]) -> ToolExecutionResult:
        tool = self._tools.get(name)
        if tool is None:
            return _error_result(name, "unknown_tool")

        try:
            request = tool.input_model.model_validate(input_data)
        except ValidationError as exc:
            return _error_result(name, "invalid_input", str(exc))

        try:
            output = await tool.handler(request)
        except Exception:  # pragma: no cover - defensive boundary for orchestration
            return _error_result(name, "execution_error")

        return ToolExecutionResult(
            tool_name=name,
            ok=True,
            output=output.model_dump(mode="json"),
        )


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
    registry = ToolRegistry()
    for bound_tool in tool_catalog.list_bound_tools():
        registry.register(
            bound_tool.contract.registry_name,
            bound_tool.contract.internal_input_model,
            bound_tool.handler,
        )
    return registry


def _error_result(name: str, code: str, message: Any | None = None) -> ToolExecutionResult:
    error_messages = {
        "unknown_tool": "unknown tool",
        "invalid_input": "tool input is invalid",
        "timeout": "tool execution timed out",
        "dependency_unavailable": "tool dependency is temporarily unavailable",
        "execution_error": "tool execution failed",
    }
    retryable = code in {"invalid_input", "timeout", "dependency_unavailable"}
    actions = {
        "unknown_tool": "stop",
        "invalid_input": "replan_arguments",
        "timeout": "retry_once",
        "dependency_unavailable": "explain_temporary_unavailability",
        "execution_error": "stop",
    }
    return ToolExecutionResult(
        tool_name=name,
        ok=False,
        error=ToolError(
            code=code,
            message=str(message or error_messages.get(code, "tool execution failed")),
            retryable=retryable,
            recommended_action=actions.get(code, "stop"),
        ),
    )
