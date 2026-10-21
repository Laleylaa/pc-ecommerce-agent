import asyncio
import re

import pytest
from app.core.config import Settings
from app.schemas.catalog_search import CatalogSearchRequest, CatalogSearchResult
from app.tools.contracts import (
    BoundTool,
    DefaultToolContractProvider,
    RegistryToolExecutor,
    ToolCatalog,
)
from app.tools.registry import build_tool_registry
from app.tools.schemas import (
    CatalogSearchOutput,
    DocumentSearchInput,
)
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

TOOL_TEST_SETTINGS = Settings(llm_api_key="", catalog_llm_planner_enabled=False)


@pytest.mark.asyncio
async def test_tool_registry_exposes_expected_business_tools() -> None:
    registry = build_tool_registry(None, settings=TOOL_TEST_SETTINGS)

    assert registry.tool_names == [
        "catalog.facets",
        "catalog.search",
        "knowledge.search",
        "order.lookup",
        "policy.search",
    ]
    assert DefaultToolContractProvider().get_contract("catalog_compare") is None
    retired = await registry.execute("catalog.compare", {"query": "比较两款键盘"})
    assert not retired.ok and retired.error.code == "unknown_tool"


def test_tool_contracts_expose_llm_safe_metadata() -> None:
    contracts = DefaultToolContractProvider().list_contracts()
    names = [contract.llm_name for contract in contracts]

    assert names == [
        "catalog_search",
        "catalog_facets",
        "order_lookup",
        "policy_search",
        "knowledge_search",
    ]
    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"[a-zA-Z0-9_]+", name) for name in names)
    assert all(contract.description for contract in contracts)
    assert all(contract.read_only for contract in contracts)
    assert {contract.name for contract in contracts if contract.parallel_safe} == {
        "catalog_search", "catalog_facets", "order_lookup", "knowledge_search",
    }

    order_contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert order_contract is not None
    assert order_contract.requires_auth is True
    assert order_contract.runtime_fields == ("user_id",)
    assert order_contract.public_input_model.model_json_schema()["additionalProperties"] is False
    assert "user_id" not in order_contract.public_input_model.model_json_schema()["properties"]


def test_tool_contracts_export_public_llm_schemas_only() -> None:
    contracts = DefaultToolContractProvider().list_contracts()

    for contract in contracts:
        schema = contract.as_llm_tool()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == contract.llm_name
        assert schema["function"]["description"] == contract.description
        parameters = schema["function"]["parameters"]
        for runtime_field in contract.runtime_fields:
            assert runtime_field not in parameters.get("properties", {})


def test_tool_contracts_align_with_registry() -> None:
    # Construction validates all real handler signatures and needs no database connection.
    registry = build_tool_registry(None, settings=TOOL_TEST_SETTINGS)

    contracts = DefaultToolContractProvider().list_contracts()
    assert sorted(contract.registry_name for contract in contracts) == registry.tool_names


def test_all_public_input_models_forbid_unknown_fields() -> None:
    contracts = DefaultToolContractProvider().list_contracts()

    for contract in contracts:
        schema = contract.public_input_model.model_json_schema()
        assert schema.get("additionalProperties") is False


def test_tool_catalog_rejects_duplicate_names_and_missing_handler() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def handler(request: CatalogSearchRequest) -> CatalogSearchResult:
        return CatalogSearchResult(
            result_type="empty", status="unsupported",
            reason_code="request_not_supported", recommended_action="explain_limitation",
        )

    with pytest.raises(ValueError, match="duplicate tool llm_name"):
        ToolCatalog([BoundTool(contract, handler), BoundTool(contract, handler)])

    with pytest.raises(ValueError, match="missing handler"):
        ToolCatalog([BoundTool(contract, None)])  # type: ignore[arg-type]


def test_tool_catalog_rejects_handler_model_mismatch() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def wrong_input_handler(request: DocumentSearchInput) -> CatalogSearchOutput:
        return CatalogSearchOutput(result_type="empty")

    async def wrong_output_handler(request: CatalogSearchRequest) -> BaseModel:
        return CatalogSearchOutput(result_type="empty")

    with pytest.raises(ValueError, match="handler input model mismatch"):
        ToolCatalog([BoundTool(contract, wrong_input_handler)])

    with pytest.raises(ValueError, match="handler output model mismatch"):
        ToolCatalog([BoundTool(contract, wrong_output_handler)])


@pytest.mark.asyncio
async def test_registry_rejects_unknown_fields_without_calling_handler() -> None:
    async def never_called_handler(request):  # pragma: no cover
        raise AssertionError("handler should not be called")

    for contract in DefaultToolContractProvider().list_contracts():
        executor = RegistryToolExecutor(
            None,  # type: ignore[arg-type]
            TOOL_TEST_SETTINGS,
            catalog=ToolCatalog([BoundTool(contract, never_called_handler)]),
        )
        args = {"unexpected_field": "must fail"}
        if "query" in contract.public_input_model.model_json_schema().get("properties", {}):
            args["query"] = "test"
        result = await executor.execute(contract, args, {"user_id": 1})
        assert not result.ok
        assert result.error is not None
        assert result.error.code == "invalid_input"
        assert result.error.retryable is True
        assert result.error.recommended_action == "replan_arguments"


@pytest.mark.asyncio
async def test_registry_tool_executor_injects_runtime_and_rewrites_tool_name() -> None:
    captured_input = None
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async def handler(request):
        nonlocal captured_input
        captured_input = request.model_dump(mode="json", exclude_none=True)
        from app.tools.schemas import OrderLookupOutput

        return OrderLookupOutput(result_type="not_found")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, handler)]),
    )

    result = await executor.execute(contract, {"order_id": 42}, {"user_id": 7})

    assert result.ok
    assert result.tool_name == "order_lookup"
    assert captured_input == {"user_id": 7, "order_id": 42, "limit": 5, "offset": 0}


@pytest.mark.asyncio
async def test_registry_tool_executor_rejects_llm_supplied_runtime_field() -> None:
    async def never_called_handler(request):  # pragma: no cover
        raise AssertionError("handler should not be called")

    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None
    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, never_called_handler)]),
    )

    result = await executor.execute(
        contract,
        {"order_id": 42, "user_id": 999},
        {"user_id": 7},
    )

    assert not result.ok
    assert result.tool_name == "order_lookup"
    assert result.error is not None
    assert result.error.code == "invalid_input"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_timeout_error() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None
    contract.timeout_seconds = 0.001

    async def slow_handler(request):
        await asyncio.sleep(0.05)
        raise AssertionError("timeout should cancel before completion")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, slow_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.tool_name == "catalog_search"
    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.error.retryable is True
    assert result.error.recommended_action == "retry_once"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_execution_error_for_unknown_exception() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def broken_handler(request):
        raise RuntimeError("postgresql://secret@localhost/internal")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, broken_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "execution_error"
    assert result.error.message == "tool execution failed"
    assert result.error.retryable is False
    assert result.error.recommended_action == "stop"


@pytest.mark.asyncio
async def test_registry_tool_executor_returns_dependency_unavailable() -> None:
    contract = DefaultToolContractProvider().get_contract("catalog_search")
    assert contract is not None

    async def unavailable_handler(request):
        raise SQLAlchemyError("postgresql://secret@localhost/internal")

    executor = RegistryToolExecutor(
        None,  # type: ignore[arg-type]
        TOOL_TEST_SETTINGS,
        catalog=ToolCatalog([BoundTool(contract, unavailable_handler)]),
    )

    result = await executor.execute(contract, {"query": "wireless mouse"}, {"user_id": 7})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "dependency_unavailable"
    assert result.error.message == "tool dependency is temporarily unavailable"
    assert result.error.retryable is True
    assert result.error.recommended_action == "explain_temporary_unavailability"


def test_stable_tool_error_codes_have_recovery_semantics() -> None:
    from app.tools.contracts import _error_result

    expected = {
        "unknown_tool": (False, "stop"),
        "invalid_input": (True, "replan_arguments"),
        "unauthorized": (False, "request_authentication"),
        "forbidden": (False, "stop"),
        "timeout": (True, "retry_once"),
        "dependency_unavailable": (True, "explain_temporary_unavailability"),
        "execution_error": (False, "stop"),
    }

    for code, (retryable, recommended_action) in expected.items():
        result = _error_result("catalog_search", code)
        assert result.error is not None
        assert result.error.code == code
        assert result.error.retryable is retryable
        assert result.error.recommended_action == recommended_action
