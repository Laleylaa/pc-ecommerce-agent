
import sqlite3
from types import SimpleNamespace

import pytest
from app.models import OrderInfo
from app.repositories import orders as order_repository
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.orders import _extract_order_id
from app.tools.schemas import OrderLookupInput, OrderLookupOutput
from pydantic import ValidationError


@pytest.mark.parametrize(
    "query,expected",
    [
        ("查订单123", 123),
        ("order ID: 42", 42),
        ("123", 123),
        ("查202607020001物流", 202607020001),
        ("查订单123和订单456", None),
        ("最近2笔订单", None),
        ("订单123的金额是否超过456元", 123),
    ],
)
def test_order_tool_and_planner_share_explicit_id_recognition(query, expected):
    assert _extract_order_id(query) == expected


@pytest.mark.parametrize("order_id", [None, 123])
@pytest.mark.parametrize("query", [None, "", " \n", "查询订单详情"])
def test_internal_order_input_matches_public_selection_requirement(order_id, query):
    args = {"order_id": order_id, "query": query}
    public = DefaultToolContractProvider().get_contract("order_lookup").public_input_model
    valid = query == "查询订单详情" or (order_id == 123 and query is None)
    for model, extra in ((public, {}), (OrderLookupInput, {"user_id": 7})):
        if valid:
            assert model.model_validate({**args, **extra}).order_id == order_id
        else:
            with pytest.raises(ValidationError):
                model.model_validate({**args, **extra})


@pytest.mark.asyncio
async def test_order_owner_predicate_and_server_identity_without_configured_database(monkeypatch):
    # Execute the real repository WHERE expression against a disposable SQLite table.
    # No ORM models, fixtures or connections from the configured database are used.
    db = sqlite3.connect(":memory:")
    table = OrderInfo.__tablename__
    db.execute(f"CREATE TABLE {table} (id INTEGER, user_id INTEGER)")
    db.executemany(f"INSERT INTO {table} VALUES (?, ?)", [(123, 7), (456, 8)])
    seen = []

    class Session:
        async def execute(self, statement):
            predicate = statement.whereclause.compile(compile_kwargs={"literal_binds": True})
            row = db.execute(f"SELECT id FROM {table} WHERE {predicate}").fetchone()
            return SimpleNamespace(scalar_one_or_none=lambda: row[0] if row else None)

    monkeypatch.setattr(order_repository, "_to_order_card", lambda identity: identity)
    repository = order_repository.OrderRepository(Session())

    async def handler(request):
        seen.append((request.user_id, request.order_id))
        assert await repository.get_order(request.user_id, request.order_id) is None
        return OrderLookupOutput(result_type="not_found")

    try:
        assert await repository.get_order(7, 123) == 123
        contract = DefaultToolContractProvider().get_contract("order_lookup")
        result = await execute_bound_tool(
            BoundTool(contract, handler), {"order_id": 456}, {"user_id": 7}
        )
        assert result.ok and result.output["result_type"] == "not_found"
        assert seen == [(7, 456)]
        forged = await execute_bound_tool(
            BoundTool(contract, handler), {"order_id": 456, "user_id": 8}, {"user_id": 7}
        )
        assert not forged.ok and forged.error.code == "invalid_input"
        assert seen == [(7, 456)]
    finally:
        db.close()
