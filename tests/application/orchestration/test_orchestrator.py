from typing import cast

import pytest
from app.core.config import Settings
from app.tools.contracts import (
    BoundTool,
    DefaultToolContractProvider,
    RegistryToolExecutor,
    ToolCatalog,
)
from app.tools.schemas import OrderLookupOutput


@pytest.mark.asyncio
async def test_order_user_id_is_injected_by_runtime() -> None:
    captured_input: dict | None = None
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    assert contract is not None

    async def handler(request) -> OrderLookupOutput:
        nonlocal captured_input
        captured_input = request.model_dump(mode="json", exclude_none=True)
        return OrderLookupOutput(result_type="not_found")

    executor = RegistryToolExecutor(
        cast(object, None),
        Settings(llm_api_key=""),
        catalog=ToolCatalog([BoundTool(contract, handler)]),
    )

    result = await executor.execute(contract, {"order_id": 42}, {"user_id": 7})

    assert result.ok is True
    assert captured_input == {"user_id": 7, "order_id": 42, "limit": 5, "offset": 0}
