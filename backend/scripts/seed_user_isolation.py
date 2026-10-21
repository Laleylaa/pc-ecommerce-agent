import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from random import Random

from app.core.database import AsyncSessionLocal
from app.models import (
    AppUser,
    Brand,
    Category,
    Conversation,
    Message,
    OrderInfo,
    OrderItem,
    OrderLogistics,
    Sku,
    Spu,
    UserAuthCredential,
)
from app.services.auth import PasswordHasher
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

USER_COUNT = 5
ORDERS_PER_USER = 5
ORDER_SKU_POOL_SIZE = USER_COUNT * ORDERS_PER_USER
ORDER_SKU_RANDOM_SEED = 20_260_724
CONVERSATIONS_PER_USER = 2
DEMO_PASSWORD = "isolation-demo-password"
ORDER_ID_BASE = 991_000_000_000
CONVERSATION_ID_BASE = 992_000_000_000
MESSAGE_ID_BASE = 993_000_000_000
ORDER_ITEM_ID_BASE = 994_000_000_000
ORDER_STATUS_LABELS = {
    1: "待付款",
    2: "待发货",
    3: "已发货",
    4: "已完成",
    5: "已关闭",
}
CATEGORY_NAME = "隔离测试外设"
BRAND_NAME = "Isolation Mock"
SPU_TITLE = "Isolation Mock 可追溯测试鼠标"
SKU_TITLE = "Isolation Mock 可追溯测试鼠标 标准版"


class SeedOwnershipError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserSeedSpec:
    ordinal: int
    username: str
    login_identifier: str
    phone: str


@dataclass(frozen=True)
class OrderSeedSpec:
    id: int
    item_id: int
    user_ordinal: int
    sequence: int
    item_price: Decimal
    total_amount: Decimal
    pay_amount: Decimal
    freight_amount: Decimal
    pay_type: int
    status: int
    created_at: datetime
    pay_at: datetime | None
    delivery_at: datetime | None


@dataclass(frozen=True)
class MessageSeedSpec:
    id: int
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class ConversationSeedSpec:
    id: int
    user_ordinal: int
    username: str
    sequence: int
    title: str
    created_at: datetime
    messages: tuple[MessageSeedSpec, MessageSeedSpec]


@dataclass(frozen=True)
class UserSummary:
    user_id: int
    username: str
    login_identifier: str


@dataclass(frozen=True)
class OrderSummary:
    order_id: int
    user_id: int
    username: str
    total_amount: Decimal
    pay_amount: Decimal
    status: int
    created_at: datetime


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: int
    user_id: int
    username: str
    title: str


@dataclass(frozen=True)
class SeedSummary:
    users: tuple[UserSummary, ...]
    orders: tuple[OrderSummary, ...]
    conversations: tuple[ConversationSummary, ...]


def build_user_specs() -> tuple[UserSeedSpec, ...]:
    return tuple(
        UserSeedSpec(
            ordinal=ordinal,
            username=f"test_user_{ordinal:03d}",
            login_identifier=f"test_user_{ordinal:03d}@example.com",
            phone=f"13900000{ordinal:03d}",
        )
        for ordinal in range(1, USER_COUNT + 1)
    )


def build_order_specs(user_ordinal: int, anchor: datetime) -> tuple[OrderSeedSpec, ...]:
    specs = []
    for sequence in range(1, ORDERS_PER_USER + 1):
        created_at = anchor - timedelta(days=(user_ordinal - 1) * ORDERS_PER_USER + sequence)
        status = sequence
        item_price = Decimal(100 + user_ordinal * 10 + sequence * 7).quantize(Decimal("0.01"))
        freight = Decimal("0.00") if sequence in {3, 4} else Decimal("10.00")
        total = item_price + freight
        paid = status in {2, 3, 4}
        shipped = status in {3, 4}
        specs.append(
            OrderSeedSpec(
                id=ORDER_ID_BASE + user_ordinal * 100 + sequence,
                item_id=ORDER_ITEM_ID_BASE + user_ordinal * 100 + sequence,
                user_ordinal=user_ordinal,
                sequence=sequence,
                item_price=item_price,
                total_amount=total,
                pay_amount=total if paid else Decimal("0.00"),
                freight_amount=freight,
                pay_type=2 if paid else 0,
                status=status,
                created_at=created_at,
                pay_at=created_at + timedelta(minutes=8) if paid else None,
                delivery_at=created_at + timedelta(hours=6) if shipped else None,
            )
        )
    return tuple(specs)


def build_conversation_specs(
    user_ordinal: int, anchor: datetime
) -> tuple[ConversationSeedSpec, ...]:
    username = f"test_user_{user_ordinal:03d}"
    specs = []
    for sequence in range(1, CONVERSATIONS_PER_USER + 1):
        conversation_id = CONVERSATION_ID_BASE + user_ordinal * 100 + sequence
        created_at = anchor - timedelta(hours=user_ordinal * 3 + sequence)
        messages = (
            MessageSeedSpec(
                id=MESSAGE_ID_BASE + user_ordinal * 1000 + sequence * 10 + 1,
                role="user",
                content=f"我是 {username}，请查询我的隔离测试订单。",
                created_at=created_at,
            ),
            MessageSeedSpec(
                id=MESSAGE_ID_BASE + user_ordinal * 1000 + sequence * 10 + 2,
                role="assistant",
                content=f"已进入 {username} 的隔离测试会话 {sequence:02d}。",
                created_at=created_at + timedelta(seconds=5),
            ),
        )
        specs.append(
            ConversationSeedSpec(
                id=conversation_id,
                user_ordinal=user_ordinal,
                username=username,
                sequence=sequence,
                title=f"{username} 隔离测试会话 {sequence:02d}",
                created_at=created_at,
                messages=messages,
            )
        )
    return tuple(specs)


def assert_expected_owner(
    record_type: str,
    record_id: int,
    actual_owner_id: int,
    expected_owner_id: int,
) -> None:
    if actual_owner_id != expected_owner_id:
        raise SeedOwnershipError(
            f"{record_type} {record_id} belongs to owner {actual_owner_id}; "
            f"expected {expected_owner_id}"
        )


def format_summary(summary: SeedSummary) -> str:
    lines = ["用户隔离 Mock 数据写入完成", "", "[users]"]
    lines.extend(
        f"user_id={item.user_id} username={item.username} "
        f"login={item.login_identifier} password={DEMO_PASSWORD}"
        for item in summary.users
    )
    lines.extend(("", "[orders]"))
    lines.extend(
        f"order_id={item.order_id} user_id={item.user_id} owner={item.username} "
        f"total_amount={item.total_amount:.2f} pay_amount={item.pay_amount:.2f} "
        f"status={item.status}({ORDER_STATUS_LABELS.get(item.status, '未知状态')}) "
        f"created_at={item.created_at.isoformat()}"
        for item in summary.orders
    )
    lines.extend(("", "[conversations]"))
    lines.extend(
        f"conversation_id={item.conversation_id} user_id={item.user_id} "
        f"owner={item.username} title={item.title}"
        for item in summary.conversations
    )
    return "\n".join(lines)


async def _upsert_users(session: AsyncSession) -> dict[int, AppUser]:
    users: dict[int, AppUser] = {}
    for spec in build_user_specs():
        user = (
            await session.execute(
                select(AppUser).where(AppUser.login_identifier == spec.login_identifier)
            )
        ).scalar_one_or_none()
        if user is None:
            user = AppUser(
                login_identifier=spec.login_identifier,
                display_name=spec.username,
                phone=spec.phone,
                status="active",
            )
            session.add(user)
        else:
            user.display_name = spec.username
            user.phone = spec.phone
            user.status = "active"
            user.updated_at = datetime.now(UTC)
        await session.flush()
        await _upsert_credential(session, user, spec)
        users[spec.ordinal] = user
    return users


async def _upsert_credential(
    session: AsyncSession,
    user: AppUser,
    spec: UserSeedSpec,
) -> None:
    credentials = (
        (
            await session.execute(
                select(UserAuthCredential).where(
                    or_(
                        UserAuthCredential.user_id == user.id,
                        UserAuthCredential.login_identifier == spec.login_identifier,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    conflicting = next(
        (credential for credential in credentials if credential.user_id != user.id),
        None,
    )
    if conflicting is not None:
        assert_expected_owner("credential", conflicting.id, conflicting.user_id, user.id)

    credential = next(
        (credential for credential in credentials if credential.user_id == user.id),
        None,
    )
    password_hash = PasswordHasher.hash_password(DEMO_PASSWORD)
    if credential is None:
        session.add(
            UserAuthCredential(
                user_id=user.id,
                login_identifier=spec.login_identifier,
                password_hash=password_hash,
            )
        )
    else:
        credential.login_identifier = spec.login_identifier
        credential.password_hash = password_hash
        credential.password_updated_at = datetime.now(UTC)
        credential.updated_at = datetime.now(UTC)
    await session.flush()


async def _get_or_create_skus(session: AsyncSession) -> tuple[Sku, ...]:
    active_skus = list(
        (
            await session.execute(
                select(Sku)
                .join(Spu, Sku.spu_id == Spu.id)
                .where(Sku.status == 1, Spu.status == 1)
                .order_by(Sku.id)
            )
        )
        .scalars()
        .all()
    )
    if active_skus:
        Random(ORDER_SKU_RANDOM_SEED).shuffle(active_skus)
        return tuple(active_skus[:ORDER_SKU_POOL_SIZE])

    category = (
        await session.execute(
            select(Category).where(Category.name == CATEGORY_NAME).order_by(Category.id).limit(1)
        )
    ).scalar_one_or_none()
    if category is None:
        category = Category(name=CATEGORY_NAME, parent_id=0, level=1)
        session.add(category)
        await session.flush()

    brand = (
        await session.execute(select(Brand).where(Brand.name == BRAND_NAME))
    ).scalar_one_or_none()
    if brand is None:
        brand = Brand(name=BRAND_NAME)
        session.add(brand)
        await session.flush()

    spu = (
        await session.execute(
            select(Spu).where(
                Spu.category_id == category.id,
                Spu.title == SPU_TITLE,
            )
        )
    ).scalar_one_or_none()
    if spu is None:
        spu = Spu(
            category_id=category.id,
            brand_id=brand.id,
            title=SPU_TITLE,
            sub_title="用户隔离 Mock 数据专用商品",
            detail_html="<p>仅用于本地用户隔离测试。</p>",
            status=1,
            sales_count=0,
        )
        session.add(spu)
        await session.flush()
    else:
        spu.brand_id = brand.id
        spu.status = 1

    sku = (
        await session.execute(select(Sku).where(Sku.spu_id == spu.id, Sku.title == SKU_TITLE))
    ).scalar_one_or_none()
    if sku is None:
        sku = Sku(
            spu_id=spu.id,
            title=SKU_TITLE,
            price=Decimal("129.00"),
            stock=999,
            sales_count=0,
            specs_json={"connection_type": "有线", "color": "测试黑"},
            status=1,
        )
        session.add(sku)
        await session.flush()
    else:
        sku.status = 1
    return (sku,)


async def _upsert_order(
    session: AsyncSession,
    user: AppUser,
    user_spec: UserSeedSpec,
    order_spec: OrderSeedSpec,
    sku: Sku,
) -> OrderInfo:
    order = await session.get(OrderInfo, order_spec.id)
    if order is None:
        order = OrderInfo(id=order_spec.id, user_id=user.id)
        session.add(order)
    else:
        assert_expected_owner("order", order.id, order.user_id, user.id)
    order.total_amount = order_spec.total_amount
    order.pay_amount = order_spec.pay_amount
    order.freight_amount = order_spec.freight_amount
    order.pay_type = order_spec.pay_type
    order.status = order_spec.status
    order.receiver_name = user_spec.username
    order.receiver_phone = user_spec.phone
    order.receiver_address = (
        f"上海市隔离测试区 {user_spec.username} 路 {order_spec.sequence:02d} 号"
    )
    order.created_at = order_spec.created_at
    order.pay_at = order_spec.pay_at
    order.delivery_at = order_spec.delivery_at
    await session.flush()
    await _upsert_order_item(session, order, order_spec, sku)
    await _upsert_logistics(session, order, user_spec.ordinal, order_spec)
    return order


async def _upsert_order_item(
    session: AsyncSession,
    order: OrderInfo,
    spec: OrderSeedSpec,
    sku: Sku,
) -> None:
    item = await session.get(OrderItem, spec.item_id)
    if item is None:
        item = OrderItem(id=spec.item_id, order_id=order.id)
        session.add(item)
    else:
        assert_expected_owner("order item", item.id, item.order_id, order.id)
    username = f"test_user_{spec.user_ordinal:03d}"
    item.spu_id = sku.spu_id
    item.sku_id = sku.id
    item.sku_name = f"{sku.title} / {username} / order-{spec.sequence:02d}"
    item.sku_specs = {
        **(sku.specs_json or {}),
        "isolation_owner": username,
        "isolation_order_sequence": spec.sequence,
    }
    item.sku_image = sku.image_url
    item.price = spec.item_price
    item.quantity = 1


async def _upsert_logistics(
    session: AsyncSession,
    order: OrderInfo,
    user_ordinal: int,
    spec: OrderSeedSpec,
) -> None:
    if spec.status not in {3, 4}:
        return
    logistics = (
        await session.execute(select(OrderLogistics).where(OrderLogistics.order_id == order.id))
    ).scalar_one_or_none()
    if logistics is None:
        logistics = OrderLogistics(order_id=order.id)
        session.add(logistics)
    delivered = spec.status == 4
    logistics.express_company = "顺丰快递"
    logistics.express_code = "SF"
    logistics.logistic_no = f"SF-{user_ordinal:03d}-{spec.sequence:02d}"
    logistics.status = 3 if delivered else 2
    logistics.trace_json = [
        {"time": spec.created_at.isoformat(), "text": "顺丰快递电子运单已创建"},
        {
            "time": spec.delivery_at.isoformat() if spec.delivery_at else "",
            "text": "顺丰快递已签收" if delivered else "顺丰快递运输中",
        },
    ]


async def _upsert_conversation(
    session: AsyncSession,
    user: AppUser,
    spec: ConversationSeedSpec,
) -> Conversation:
    conversation = await session.get(Conversation, spec.id)
    if conversation is None:
        conversation = Conversation(id=spec.id, user_id=user.id)
        session.add(conversation)
    else:
        assert_expected_owner("conversation", conversation.id, conversation.user_id, user.id)
    conversation.title = spec.title
    conversation.working_memory_json = {
        "isolation_test": {"marker": "user-isolation-mock", "owner": spec.username}
    }
    conversation.created_at = spec.created_at
    conversation.updated_at = spec.messages[-1].created_at
    await session.flush()
    for message_spec in spec.messages:
        await _upsert_message(session, conversation, message_spec)
    return conversation


async def _upsert_message(
    session: AsyncSession,
    conversation: Conversation,
    spec: MessageSeedSpec,
) -> None:
    message = await session.get(Message, spec.id)
    if message is None:
        message = Message(id=spec.id, conversation_id=conversation.id)
        session.add(message)
    else:
        assert_expected_owner("message", message.id, message.conversation_id, conversation.id)
    message.role = spec.role
    message.content = spec.content
    message.metadata_json = {
        "marker": "user-isolation-mock",
        "owner": conversation.user_id,
    }
    message.created_at = spec.created_at


async def _resolve_anchor(
    session: AsyncSession,
    anchor: datetime | None,
) -> datetime:
    resolved_anchor = anchor
    if resolved_anchor is None:
        first_order_id = ORDER_ID_BASE + 101
        first_order = await session.get(OrderInfo, first_order_id)
        resolved_anchor = (
            first_order.created_at + timedelta(days=1)
            if first_order is not None
            else datetime.now(UTC).replace(tzinfo=None)
        )
    return resolved_anchor.replace(tzinfo=None, second=0, microsecond=0)


async def seed_user_isolation(
    session: AsyncSession,
    anchor: datetime | None = None,
) -> SeedSummary:
    normalized_anchor = await _resolve_anchor(session, anchor)
    user_specs = build_user_specs()
    users = await _upsert_users(session)
    skus = await _get_or_create_skus(session)
    order_summaries = []
    conversation_summaries = []

    for user_spec in user_specs:
        user = users[user_spec.ordinal]
        for order_spec in build_order_specs(user_spec.ordinal, normalized_anchor):
            sku_index = ((user_spec.ordinal - 1) * ORDERS_PER_USER + order_spec.sequence - 1) % len(
                skus
            )
            sku = skus[sku_index]
            order = await _upsert_order(session, user, user_spec, order_spec, sku)
            order_summaries.append(
                OrderSummary(
                    order_id=order.id,
                    user_id=user.id,
                    username=user_spec.username,
                    total_amount=order.total_amount,
                    pay_amount=order.pay_amount,
                    status=order.status,
                    created_at=order.created_at,
                )
            )
        for conversation_spec in build_conversation_specs(
            user_spec.ordinal,
            normalized_anchor,
        ):
            conversation = await _upsert_conversation(session, user, conversation_spec)
            conversation_summaries.append(
                ConversationSummary(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    username=user_spec.username,
                    title=conversation.title,
                )
            )

    return SeedSummary(
        users=tuple(
            UserSummary(
                user_id=users[spec.ordinal].id,
                username=spec.username,
                login_identifier=spec.login_identifier,
            )
            for spec in user_specs
        ),
        orders=tuple(order_summaries),
        conversations=tuple(conversation_summaries),
    )


async def seed_in_transaction() -> SeedSummary:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            summary = await seed_user_isolation(session)
        return summary


def main() -> int:
    try:
        summary = asyncio.run(seed_in_transaction())
    except Exception as exc:
        print(
            f"用户隔离 Mock 数据写入失败: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
