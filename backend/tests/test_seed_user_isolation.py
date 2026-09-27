from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppUser,
    Conversation,
    Message,
    OrderInfo,
    OrderItem,
    UserAuthCredential,
    UserSession,
)
from app.services.auth import PasswordHasher
from scripts.seed_user_isolation import (
    DEMO_PASSWORD,
    ConversationSummary,
    OrderSummary,
    SeedOwnershipError,
    SeedSummary,
    UserSummary,
    assert_expected_owner,
    build_conversation_specs,
    build_order_specs,
    build_user_specs,
    format_summary,
    seed_user_isolation,
)

ANCHOR = datetime(2026, 7, 14, 12, 0, 0)


def test_build_user_specs_creates_five_traceable_login_accounts() -> None:
    specs = build_user_specs()

    assert [spec.username for spec in specs] == [
        "test_user_001",
        "test_user_002",
        "test_user_003",
        "test_user_004",
        "test_user_005",
    ]
    assert specs[0].login_identifier == "test_user_001@example.com"
    assert specs[-1].phone == "13900000005"


def test_build_order_specs_creates_five_owned_orders_with_stable_ids() -> None:
    specs = build_order_specs(3, ANCHOR)

    assert len(specs) == 5
    assert [spec.id for spec in specs] == [
        991000000301,
        991000000302,
        991000000303,
        991000000304,
        991000000305,
    ]
    assert [spec.status for spec in specs] == [1, 2, 3, 4, 5]
    assert all(spec.user_ordinal == 3 for spec in specs)
    assert all(spec.total_amount == spec.item_price + spec.freight_amount for spec in specs)


def test_order_payment_and_delivery_times_match_status() -> None:
    pending, waiting, shipped, completed, closed = build_order_specs(1, ANCHOR)

    assert pending.pay_amount == Decimal("0.00")
    assert pending.pay_at is None and pending.delivery_at is None
    assert waiting.pay_amount == waiting.total_amount
    assert waiting.pay_at is not None and waiting.delivery_at is None
    assert shipped.pay_at is not None and shipped.delivery_at is not None
    assert completed.pay_at is not None and completed.delivery_at is not None
    assert closed.pay_amount == Decimal("0.00")
    assert closed.pay_at is None and closed.delivery_at is None


def test_build_conversation_specs_creates_two_conversations_with_messages() -> None:
    specs = build_conversation_specs(2, ANCHOR)

    assert [spec.id for spec in specs] == [992000000201, 992000000202]
    assert all(spec.username == "test_user_002" for spec in specs)
    assert all(len(spec.messages) == 2 for spec in specs)
    assert [message.role for message in specs[0].messages] == ["user", "assistant"]
    assert "test_user_002" in specs[0].messages[0].content


def test_assert_expected_owner_rejects_namespaced_id_collision() -> None:
    with pytest.raises(SeedOwnershipError, match="belongs to owner 20"):
        assert_expected_owner("order", 991000000101, 20, 10)


def test_format_summary_lists_credentials_orders_and_conversations() -> None:
    summary = SeedSummary(
        users=(
            UserSummary(
                user_id=10,
                username="test_user_001",
                login_identifier="test_user_001@example.com",
            ),
        ),
        orders=(
            OrderSummary(
                order_id=991000000101,
                user_id=10,
                username="test_user_001",
                total_amount=Decimal("130.00"),
                pay_amount=Decimal("120.00"),
                status=2,
                created_at=ANCHOR,
            ),
        ),
        conversations=(
            ConversationSummary(
                conversation_id=992000000101,
                user_id=10,
                username="test_user_001",
                title="test_user_001 隔离测试会话 01",
            ),
        ),
    )

    output = format_summary(summary)

    assert f"password={DEMO_PASSWORD}" in output
    assert "order_id=991000000101 user_id=10 owner=test_user_001" in output
    assert "total_amount=130.00 pay_amount=120.00 status=2(待发货)" in output
    assert "conversation_id=992000000101 user_id=10 owner=test_user_001" in output


@pytest.mark.asyncio
async def test_seed_user_isolation_persists_owned_data_idempotently(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_specs = build_user_specs()
    login_identifiers = [spec.login_identifier for spec in user_specs]
    order_specs = [
        order_spec
        for user_spec in user_specs
        for order_spec in build_order_specs(user_spec.ordinal, ANCHOR)
    ]
    order_ids = [spec.id for spec in order_specs]
    order_item_ids = [spec.item_id for spec in order_specs]
    conversation_specs = [
        conversation_spec
        for user_spec in user_specs
        for conversation_spec in build_conversation_specs(user_spec.ordinal, ANCHOR)
    ]
    conversation_ids = [spec.id for spec in conversation_specs]
    message_ids = [
        message.id
        for conversation_spec in conversation_specs
        for message in conversation_spec.messages
    ]

    async with db_session_factory() as session:
        existing_session_count = await session.scalar(
            select(func.count(UserSession.id))
            .join(AppUser, UserSession.user_id == AppUser.id)
            .where(AppUser.login_identifier.in_(login_identifiers))
        )
        first = await seed_user_isolation(session, ANCHOR)
        await session.commit()
    async with db_session_factory() as session:
        second = await seed_user_isolation(session)
        await session.commit()

        users = (
            (
                await session.execute(
                    select(AppUser).where(AppUser.login_identifier.in_(login_identifiers))
                )
            )
            .scalars()
            .all()
        )
        user_ids = {user.id for user in users}
        credentials = (
            (
                await session.execute(
                    select(UserAuthCredential).where(UserAuthCredential.user_id.in_(user_ids))
                )
            )
            .scalars()
            .all()
        )
        order_counts = dict(
            (
                await session.execute(
                    select(OrderInfo.user_id, func.count(OrderInfo.id))
                    .where(OrderInfo.id.in_(order_ids))
                    .group_by(OrderInfo.user_id)
                )
            ).all()
        )
        conversation_counts = dict(
            (
                await session.execute(
                    select(Conversation.user_id, func.count(Conversation.id))
                    .where(Conversation.id.in_(conversation_ids))
                    .group_by(Conversation.user_id)
                )
            ).all()
        )
        item_count = await session.scalar(
            select(func.count(OrderItem.id)).where(OrderItem.id.in_(order_item_ids))
        )
        distinct_sku_counts = dict(
            (
                await session.execute(
                    select(
                        OrderInfo.user_id,
                        func.count(func.distinct(OrderItem.sku_id)),
                    )
                    .join(OrderItem, OrderItem.order_id == OrderInfo.id)
                    .where(OrderInfo.id.in_(order_ids))
                    .group_by(OrderInfo.user_id)
                )
            ).all()
        )
        message_count = await session.scalar(
            select(func.count(Message.id)).where(Message.id.in_(message_ids))
        )
        session_count = await session.scalar(
            select(func.count(UserSession.id)).where(UserSession.user_id.in_(user_ids))
        )

    assert len(first.users) == len(second.users) == 5
    assert len(first.orders) == len(second.orders) == 25
    assert len(first.conversations) == len(second.conversations) == 10
    assert [item.created_at for item in second.orders] == [
        item.created_at for item in first.orders
    ]
    assert len(users) == len(credentials) == 5
    assert all(
        PasswordHasher.verify_password(DEMO_PASSWORD, credential.password_hash)
        for credential in credentials
    )
    assert set(order_counts) == set(conversation_counts) == user_ids
    assert set(order_counts.values()) == {5}
    assert set(conversation_counts.values()) == {2}
    assert item_count == 25
    assert set(distinct_sku_counts.values()) == {5}
    assert message_count == 20
    assert session_count == existing_session_count


def test_main_prints_summary_only_after_success(monkeypatch, capsys) -> None:
    summary = SeedSummary(users=(), orders=(), conversations=())

    async def successful_seed() -> SeedSummary:
        return summary

    monkeypatch.setattr(
        "scripts.seed_user_isolation.seed_in_transaction",
        successful_seed,
    )

    from scripts.seed_user_isolation import main

    assert main() == 0
    captured = capsys.readouterr()
    assert "用户隔离 Mock 数据写入完成" in captured.out
    assert captured.err == ""


def test_main_reports_failure_without_success_summary(monkeypatch, capsys) -> None:
    async def failed_seed() -> SeedSummary:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "scripts.seed_user_isolation.seed_in_transaction",
        failed_seed,
    )

    from scripts.seed_user_isolation import main

    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "database unavailable" in captured.err
