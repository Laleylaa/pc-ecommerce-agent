import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.llm import build_chat_model
from app.tools.catalog import CatalogQueryPlanner, CatalogToolService, LLMCatalogQueryPlanner
from app.tools.knowledge import KnowledgeRetrievalToolService
from app.tools.orders import LLMOrderQueryPlanner, OrderQueryPlanner, OrderToolService
from app.tools.schemas import (
    CatalogCompareInput,
    CatalogCompareOutput,
    CatalogFacetInput,
    CatalogFacetOutput,
    CatalogSearchInput,
    CatalogSearchOutput,
    CatalogTargetIdentity,
    CatalogToolRuntimeContext,
    DocumentSearchInput,
    DocumentSearchOutput,
    OrderLookupInput,
    OrderLookupOutput,
    ToolError,
    ToolExecutionResult,
)


class ToolRuntimeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    targets: list[CatalogTargetIdentity] = Field(default_factory=list, max_length=10)
    comparison_level: Literal["auto", "sku", "spu"] = "auto"
    catalog_context: CatalogToolRuntimeContext = Field(
        default_factory=CatalogToolRuntimeContext
    )


class CatalogSearchPublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    limit: int = Field(default=3, ge=1, le=20)


class CatalogComparePublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=2, le=10)


class CatalogFacetPublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=50)


class OrderLookupPublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int | None = None
    query: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=5, ge=1, le=5)
    offset: int = Field(default=0, ge=0, le=10000)


class DocumentSearchPublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    document_type: str | None = None
    limit: int = Field(default=3, ge=1, le=10)


class ToolContract(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_name: str
    registry_name: str
    description: str
    public_input_model: type[BaseModel]
    internal_input_model: type[BaseModel]
    output_model: type[BaseModel]
    runtime_fields: tuple[str, ...] = ()
    read_only: bool = True
    parallel_safe: bool = False
    requires_auth: bool = False
    timeout_seconds: float | None = 12.0

    @property
    def name(self) -> str:
        return self.llm_name

    def as_llm_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.llm_name,
                "description": self.description,
                "parameters": self.public_input_model.model_json_schema(),
            },
        }


ToolHandler = Callable[[BaseModel], Awaitable[BaseModel]]


@dataclass(frozen=True)
class BoundTool:
    contract: ToolContract
    handler: ToolHandler


class ToolCatalog:
    def __init__(self, bound_tools: Sequence[BoundTool]):
        self._by_llm_name: dict[str, BoundTool] = {}
        self._by_registry_name: dict[str, BoundTool] = {}
        for bound_tool in bound_tools:
            self._validate_bound_tool(bound_tool)
            contract = bound_tool.contract
            if contract.llm_name in self._by_llm_name:
                raise ValueError(f"duplicate tool llm_name: {contract.llm_name}")
            if contract.registry_name in self._by_registry_name:
                raise ValueError(f"duplicate tool registry_name: {contract.registry_name}")
            self._by_llm_name[contract.llm_name] = bound_tool
            self._by_registry_name[contract.registry_name] = bound_tool

    @staticmethod
    def _validate_bound_tool(bound_tool: BoundTool) -> None:
        contract = bound_tool.contract
        handler = bound_tool.handler
        if not callable(handler):
            raise ValueError(f"missing handler for tool: {contract.llm_name}")

        signature = inspect.signature(handler)
        positional_params = [
            param
            for param in signature.parameters.values()
            if param.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ]
        required_positional = [
            param for param in positional_params if param.default is inspect.Parameter.empty
        ]
        has_var_positional = any(
            param.kind == inspect.Parameter.VAR_POSITIONAL
            for param in signature.parameters.values()
        )
        if len(required_positional) != 1 and not has_var_positional:
            raise ValueError(f"handler for {contract.llm_name} must accept exactly one request")

        hints = get_type_hints(handler)
        request_annotation = hints.get(required_positional[0].name) if required_positional else None
        if (
            request_annotation is not None
            and request_annotation is not contract.internal_input_model
        ):
            raise ValueError(f"handler input model mismatch for tool: {contract.llm_name}")

        return_annotation = hints.get("return")
        if return_annotation is not None and return_annotation is not contract.output_model:
            raise ValueError(f"handler output model mismatch for tool: {contract.llm_name}")

    def list_bound_tools(self) -> Sequence[BoundTool]:
        return tuple(self._by_llm_name.values())

    def list_contracts(self) -> Sequence[ToolContract]:
        return tuple(bound_tool.contract for bound_tool in self._by_llm_name.values())

    def get_by_llm_name(self, llm_name: str) -> BoundTool | None:
        return self._by_llm_name.get(llm_name)

    def get_by_registry_name(self, registry_name: str) -> BoundTool | None:
        return self._by_registry_name.get(registry_name)


class ToolContractProvider(Protocol):
    def list_contracts(self) -> Sequence[ToolContract]: ...

    def get_contract(self, llm_name: str) -> ToolContract | None: ...


class ToolExecutor(Protocol):
    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult: ...


class DefaultToolContractProvider:
    """Provide built-in contracts from the authoritative tool definitions."""

    def __init__(self, catalog: ToolCatalog | None = None):
        self.catalog = catalog or static_tool_catalog()

    def list_contracts(self) -> Sequence[ToolContract]:
        return self.catalog.list_contracts()

    def get_contract(self, llm_name: str) -> ToolContract | None:
        bound_tool = self.catalog.get_by_llm_name(llm_name)
        return bound_tool.contract if bound_tool else None


class RegistryToolExecutor:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        catalog: ToolCatalog | None = None,
        catalog_session_factory: async_sessionmaker[AsyncSession] | None = None,
    ):
        self.catalog = catalog or build_tool_catalog(
            session,
            settings=settings,
            catalog_session_factory=catalog_session_factory,
        )

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult:
        bound_tool = self.catalog.get_by_llm_name(contract.llm_name)
        if bound_tool is None:
            return _error_result(contract.llm_name, "unknown_tool")
        return await execute_bound_tool(bound_tool, arguments, runtime_context)


LLM_SAFE_TOOL_NAMES = (
    "catalog_search",
    "catalog_compare",
    "catalog_facets",
    "order_lookup",
    "policy_search",
    "knowledge_search",
)


def static_tool_catalog() -> ToolCatalog:
    def make_unbound_handler(contract: ToolContract) -> ToolHandler:
        async def _unbound_handler(_: BaseModel) -> BaseModel:  # pragma: no cover
            raise RuntimeError("static tool catalog has no runtime handler")

        _unbound_handler.__annotations__ = {
            "_": contract.internal_input_model,
            "return": contract.output_model,
        }
        return _unbound_handler

    return ToolCatalog(
        [BoundTool(contract, make_unbound_handler(contract)) for contract in _tool_contracts()]
    )


def build_tool_catalog(
    session: AsyncSession,
    *,
    settings: Settings,
    catalog_planner: CatalogQueryPlanner | None = None,
    order_planner: OrderQueryPlanner | None = None,
    catalog_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ToolCatalog:
    if catalog_session_factory is None:
        from app.core.database import AsyncSessionLocal

        catalog_session_factory = AsyncSessionLocal
    catalog_service = CatalogToolService(
        session,
        planner=catalog_planner or build_catalog_planner(settings),
        session_factory=catalog_session_factory,
    )
    orders = OrderToolService(
        session,
        planner=order_planner or build_order_planner(settings),
    )
    knowledge = KnowledgeRetrievalToolService()
    handlers: dict[str, ToolHandler] = {
        "catalog_search": catalog_service.search,  # type: ignore[dict-item]
        "catalog_compare": catalog_service.compare,  # type: ignore[dict-item]
        "catalog_facets": catalog_service.facets,  # type: ignore[dict-item]
        "order_lookup": orders.lookup,  # type: ignore[dict-item]
        "policy_search": knowledge.search_policy,  # type: ignore[dict-item]
        "knowledge_search": knowledge.search_knowledge,  # type: ignore[dict-item]
    }
    return ToolCatalog(
        [BoundTool(contract, handlers[contract.llm_name]) for contract in _tool_contracts()]
    )


def build_catalog_planner(settings: Settings | None = None) -> CatalogQueryPlanner | None:
    from app.core.config import get_settings

    settings = settings or get_settings()
    if not settings.catalog_llm_planner_enabled:
        return None

    chat_model = build_chat_model(settings)
    if chat_model is None:
        return None

    return LLMCatalogQueryPlanner(chat_model)


def build_order_planner(settings: Settings | None = None) -> OrderQueryPlanner | None:
    from app.core.config import get_settings

    settings = settings or get_settings()
    chat_model = build_chat_model(settings)
    if chat_model is None:
        return None
    return LLMOrderQueryPlanner(chat_model)


async def execute_bound_tool(
    bound_tool: BoundTool,
    arguments: dict[str, Any],
    runtime_context: dict[str, Any],
) -> ToolExecutionResult:
    contract = bound_tool.contract
    try:
        public_input = contract.public_input_model.model_validate(arguments)
        internal_input = public_input.model_dump(mode="json", exclude_none=True)
        runtime = ToolRuntimeContext.model_validate(runtime_context)
        for field_name in contract.runtime_fields:
            internal_input[field_name] = getattr(runtime, field_name)
        validated_internal = contract.internal_input_model.model_validate(internal_input)
    except ValidationError as exc:
        return _error_result(contract.llm_name, "invalid_input", str(exc))
    except AttributeError as exc:
        return _error_result(contract.llm_name, "invalid_input", str(exc))

    async def run_tool() -> BaseModel:
        return await bound_tool.handler(validated_internal)

    try:
        if contract.timeout_seconds is None:
            output = await run_tool()
        else:
            output = await asyncio.wait_for(run_tool(), timeout=contract.timeout_seconds)
    except TimeoutError:
        return _error_result(contract.llm_name, "timeout")
    except (SQLAlchemyError, OSError):
        return _error_result(contract.llm_name, "dependency_unavailable")
    except Exception:
        return _error_result(contract.llm_name, "execution_error")

    try:
        validated_output = contract.output_model.model_validate(output.model_dump(mode="python"))
    except ValidationError:
        return _error_result(contract.llm_name, "execution_error")

    return ToolExecutionResult(
        tool_name=contract.llm_name,
        ok=True,
        output=validated_output.model_dump(mode="json"),
    )


def _tool_contracts() -> tuple[ToolContract, ...]:
    return (
        ToolContract(
            llm_name="catalog_search",
            registry_name="catalog.search",
            description=(
                "Search the current PC peripheral catalog for product recommendations, "
                "category, brand, budget, connection type, stock, price, SKU/SPU sales counts, "
                "and specification facts. Use catalog_compare instead for product-versus-"
                "product comparisons. Do not use this tool for policies or order data."
            ),
            public_input_model=CatalogSearchPublicInput,
            internal_input_model=CatalogSearchInput,
            output_model=CatalogSearchOutput,
            runtime_fields=("catalog_context",),
            timeout_seconds=15.0,
        ),
        ToolContract(
            llm_name="catalog_compare",
            registry_name="catalog.compare",
            description=(
                "Compare explicit SKUs or whole SPU product series. SKU mode compares exact "
                "variants; SPU mode aggregates every active SKU into price ranges, shared "
                "specifications, available options, and real variant combinations. Return "
                "comparison evidence only; do not make final purchase promises."
            ),
            public_input_model=CatalogComparePublicInput,
            internal_input_model=CatalogCompareInput,
            output_model=CatalogCompareOutput,
            runtime_fields=("catalog_context",),
            timeout_seconds=18.0,
        ),
        ToolContract(
            llm_name="catalog_facets",
            registry_name="catalog.facets",
            description=(
                "List catalog metadata and counts such as available brands in a category, "
                "product categories sold by a brand, specification keys, or available values "
                "for a specification. Query-first usage is supported: pass the user utterance "
                "and the tool will infer facet, category, brand, and spec_key when possible. "
                "Do not use it for product lists."
            ),
            public_input_model=CatalogFacetPublicInput,
            internal_input_model=CatalogFacetInput,
            output_model=CatalogFacetOutput,
            runtime_fields=("catalog_context",),
            timeout_seconds=8.0,
        ),
        ToolContract(
            llm_name="order_lookup",
            registry_name="order.lookup",
            description=(
                "Look up the authenticated user's order count, latest order, recent orders, "
                "all five orders, remaining orders from the previous result, one explicit "
                "order, or answer a purchase-history and cross-order analysis question from "
                "a safe exhaustive order snapshot. Pass "
                "order_id only when it is explicit; otherwise preserve the user's full query so "
                "the tool can infer count and window semantics. Runtime injects user_id; never "
                "ask the model to provide or override user identity."
            ),
            public_input_model=OrderLookupPublicInput,
            internal_input_model=OrderLookupInput,
            output_model=OrderLookupOutput,
            runtime_fields=("user_id",),
            requires_auth=True,
            timeout_seconds=8.0,
        ),
        ToolContract(
            llm_name="policy_search",
            registry_name="policy.search",
            description=(
                "Search read-only store policies for returns, refunds, warranty, invoices, "
                "shipping, price protection, and after-sales procedures. Return policy "
                "evidence only; do not approve refunds or perform after-sales actions."
            ),
            public_input_model=DocumentSearchPublicInput,
            internal_input_model=DocumentSearchInput,
            output_model=DocumentSearchOutput,
            timeout_seconds=8.0,
        ),
        ToolContract(
            llm_name="knowledge_search",
            registry_name="knowledge.search",
            description=(
                "Search read-only PC peripheral, brand, merchant, purchasing, and FAQ "
                "knowledge. Do not use it for current product price, stock, or order facts."
            ),
            public_input_model=DocumentSearchPublicInput,
            internal_input_model=DocumentSearchInput,
            output_model=DocumentSearchOutput,
            timeout_seconds=8.0,
        ),
    )


def default_tool_contracts() -> tuple[ToolContract, ...]:
    return _tool_contracts()


def _error_result(name: str, code: str, message: Any | None = None) -> ToolExecutionResult:
    error_messages = {
        "unknown_tool": "unknown tool",
        "invalid_input": "tool input is invalid",
        "unauthorized": "authentication is required",
        "forbidden": "access is forbidden",
        "timeout": "tool execution timed out",
        "dependency_unavailable": "tool dependency is temporarily unavailable",
        "execution_error": "tool execution failed",
    }
    retryable = code in {"invalid_input", "timeout", "dependency_unavailable"}
    actions = {
        "unknown_tool": "stop",
        "invalid_input": "replan_arguments",
        "unauthorized": "request_authentication",
        "forbidden": "stop",
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
