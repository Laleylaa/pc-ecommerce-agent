"""Demo safety and evidence consistency; no PostgreSQL or real credentials."""

import json
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.models import AppUser, OrderInfo, OrderItem, OrderLogistics, Sku
from app.services.knowledge_documents import read_documents
from scripts.demo import load, policies
from scripts.demo.accounts import DEMO_LOGINS, create_accounts
from scripts.demo.orders import create_orders


@pytest.mark.asyncio
async def test_populated_database_rejected_before_any_seed(monkeypatch):
    session = SimpleNamespace(execute=AsyncMock(return_value=Mock(first=lambda: (1,))))
    writer = AsyncMock()
    monkeypatch.setattr(load, "upsert_product", writer)
    with pytest.raises(load.DemoDatabaseNotEmpty, match="no demo data was changed"):
        await load.load_demo(session, datetime(2026, 10, 27))
    writer.assert_not_called()


@pytest.mark.asyncio
async def test_orders_use_catalog_facts_and_one_consistent_clock():
    objects = []
    session = SimpleNamespace(add=objects.append, flush=AsyncMock())
    users = [AppUser(id=i, display_name=f"Demo {i}") for i in range(1, 7)]
    skus = [Sku(id=1, spu_id=2, title="演示鼠标", price=Decimal("129.00"),
                specs_json={"color": "黑色", "weight_g": 58})]
    anchor = datetime(2026, 10, 27, 12)
    assert await create_orders(session, users, skus, anchor) == 26
    orders = {o.id: o for o in objects if isinstance(o, OrderInfo)}
    assert len(orders) == 26
    assert {o.user_id for o in orders.values()} == set(range(1, 7))
    assert {o.status for o in orders.values()} == {1, 2, 3, 4, 5}
    for obj in objects:
        if isinstance(obj, OrderItem):
            assert obj.price == skus[0].price
            assert obj.sku_specs == skus[0].specs_json
            assert obj.sku_name == skus[0].title
        if isinstance(obj, OrderLogistics):
            order = orders[obj.order_id]
            times = [datetime.fromisoformat(event["time"]) for event in obj.trace_json]
            assert order.created_at < order.pay_at < order.delivery_at <= times[0]
            assert times[0] < times[1] <= anchor


@pytest.mark.asyncio
async def test_public_accounts_do_not_create_test_conversations():
    objects = []

    async def flush():
        for i, obj in enumerate(objects, 1):
            if isinstance(obj, AppUser) and obj.id is None:
                obj.id = i

    session = SimpleNamespace(add=objects.append, flush=flush)
    users = await create_accounts(session)
    assert len(users) == 6
    assert tuple(u.login_identifier for u in users) == DEMO_LOGINS
    assert {type(o).__name__ for o in objects} == {"AppUser", "UserAuthCredential"}


@pytest.mark.asyncio
async def test_imported_policies_match_retrieval_source_exactly():
    objects = []
    session = SimpleNamespace(
        add=objects.append,
        execute=AsyncMock(return_value=Mock(scalar_one_or_none=lambda: None)),
    )
    await policies.load_policies(session)
    expected = [(d.title, d.document_type, d.content, d.metadata) for d in read_documents()]
    assert [(d.title, d.document_type, d.content, d.metadata_json) for d in objects] == expected


@pytest.mark.asyncio
async def test_policy_conflict_is_not_overwritten():
    existing = SimpleNamespace(
        content="Local custom policy", document_type="policy", metadata_json={},
    )
    session = SimpleNamespace(
        add=Mock(), execute=AsyncMock(return_value=Mock(scalar_one_or_none=lambda: existing)),
    )
    with pytest.raises(ValueError, match="conflicts"):
        await policies.load_policies(session)
    assert existing.content == "Local custom policy"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_failure_rolls_back_entire_demo_initialization(monkeypatch):
    from app.core import database

    transaction = AsyncMock()
    session = SimpleNamespace(begin=Mock(return_value=transaction))
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(database, "AsyncSessionLocal", Mock(return_value=context))
    monkeypatch.setattr(load, "load_demo", AsyncMock(side_effect=ValueError("broken input")))
    with pytest.raises(ValueError, match="broken input"):
        await load.initialize()
    assert transaction.__aexit__.call_args.args[0] is ValueError


def test_demo_manifest_matches_account_and_catalog_inputs():
    from scripts.demo.catalog import build_compact_catalog, summarize_catalog

    manifest = json.loads(load.MANIFEST_PATH.read_text())
    catalog = summarize_catalog(build_compact_catalog())
    for name in ("categories", "spus", "skus"):
        assert manifest[name] == catalog[name]
    assert manifest["users"] == len(DEMO_LOGINS) == 6
