import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.database import AsyncSessionLocal
from app.models import (
    AppUser,
    OrderInfo,
    OrderItem,
    OrderLogistics,
    Sku,
    Spu,
    UserAuthCredential,
)
from app.services.auth import PasswordHasher, normalize_login_identifier
from app.services.dataset_mapper import normalize_part_record
from sqlalchemy import select, text

from scripts.demo.catalog_store import upsert_product
from scripts.demo.policies import load_policies
from scripts.seed_user_isolation import seed_user_isolation

DEMO_LOGIN_IDENTIFIER = "demo@example.com"
DEMO_PASSWORD = "demo-password"

DEMO_PARTS = [
    (
        "mouse",
        {
            "name": "Logitech G502 Hero",
            "price": 44.77,
            "tracking_method": "Optical",
            "connection_type": "Wired",
            "max_dpi": 25600,
            "hand_orientation": "Right",
            "color": "Black",
        },
    ),
    (
        "mouse",
        {
            "name": "Razer Viper V3 Pro",
            "price": 139.99,
            "tracking_method": "Optical",
            "connection_type": "Wired, Wireless",
            "max_dpi": 35000,
            "hand_orientation": "Right",
            "color": "White",
        },
    ),
    (
        "keyboard",
        {
            "name": "RK Royal Kludge RK61",
            "price": 49.99,
            "style": "Mini",
            "switches": "RK Red",
            "backlit": "White",
            "tenkeyless": True,
            "connection_type": "Wired, Wireless, Bluetooth Wireless",
            "color": "White",
        },
    ),
    (
        "keyboard",
        {
            "name": "Razer Huntsman Mini",
            "price": 89,
            "style": "Mini",
            "switches": "Razer Red Optical Linear",
            "backlit": "RGB",
            "tenkeyless": True,
            "connection_type": "Wired",
            "color": "Black",
        },
    ),
    (
        "headphones",
        {
            "name": "Logitech G PRO X 2",
            "price": 199.99,
            "type": "Circumaural",
            "microphone": True,
            "wireless": True,
            "enclosure_type": "Closed",
            "color": "Black",
        },
    ),
]


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def main() -> None:
    async with AsyncSessionLocal() as session:
        user = await _get_or_create_user(session)
        await _ensure_user_auth_credential(session, user)
        first_sku = await _get_first_active_sku(session)
        if first_sku is None:
            imported = [
                product
                for part_type, record in DEMO_PARTS
                if (product := normalize_part_record(part_type, record)) is not None
            ]
            for product in imported:
                sku = await upsert_product(session, product)
                first_sku = first_sku or sku
        if first_sku:
            await _seed_order(session, user.id, first_sku)
        await seed_user_isolation(session)
        await load_policies(session)
        await session.commit()


async def _get_or_create_user(session):
    user = await session.get(AppUser, 1)
    if user:
        user.login_identifier = normalize_login_identifier(DEMO_LOGIN_IDENTIFIER)
        user.status = "active"
        user.updated_at = datetime.now(UTC)
        await _sync_app_user_id_sequence(session)
        return user
    user = AppUser(
        id=1,
        login_identifier=normalize_login_identifier(DEMO_LOGIN_IDENTIFIER),
        display_name="Demo 用户",
        phone="13800000000",
        status="active",
    )
    session.add(user)
    await session.flush()
    await _sync_app_user_id_sequence(session)
    return user


async def _ensure_user_auth_credential(
    session,
    user: AppUser,
    password: str = DEMO_PASSWORD,
) -> UserAuthCredential:
    login_identifier = normalize_login_identifier(user.login_identifier)
    credential = (
        await session.execute(
            select(UserAuthCredential).where(UserAuthCredential.user_id == user.id)
        )
    ).scalar_one_or_none()
    password_hash = PasswordHasher.hash_password(password)
    if credential:
        credential.login_identifier = login_identifier
        credential.password_hash = password_hash
        credential.updated_at = datetime.now(UTC)
        return credential
    credential = UserAuthCredential(
        user_id=user.id,
        login_identifier=login_identifier,
        password_hash=password_hash,
    )
    session.add(credential)
    await session.flush()
    return credential


async def _sync_app_user_id_sequence(session) -> None:
    await session.execute(
        text(
            """
            SELECT setval(
              pg_get_serial_sequence('app_user', 'id'),
              GREATEST((SELECT COALESCE(MAX(id), 1) FROM app_user), 1)
            )
            """
        )
    )


async def _get_first_active_sku(session) -> Sku | None:
    return (
        await session.execute(
            select(Sku)
            .join(Spu, Sku.spu_id == Spu.id)
            .where(Sku.status == 1, Spu.status == 1)
            .order_by(Sku.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _seed_order(session, user_id: int, sku: Sku) -> None:
    order_id = 202607020001
    if await session.get(OrderInfo, order_id):
        return
    created_at = utc_now_naive() - timedelta(days=1)
    order = OrderInfo(
        id=order_id,
        user_id=user_id,
        total_amount=Decimal(sku.price),
        pay_amount=Decimal(sku.price),
        freight_amount=Decimal("0"),
        pay_type=2,
        status=3,
        receiver_name="Demo 用户",
        receiver_phone="13800000000",
        receiver_address="上海市浦东新区 Demo 路 100 号",
        created_at=created_at,
        pay_at=created_at + timedelta(minutes=5),
        delivery_at=created_at + timedelta(hours=6),
    )
    session.add(order)
    await session.flush()
    session.add(
        OrderItem(
            order_id=order.id,
            spu_id=sku.spu_id,
            sku_id=sku.id,
            sku_name=sku.title,
            sku_specs=sku.specs_json,
            sku_image=sku.image_url,
            price=sku.price,
            quantity=1,
        )
    )
    session.add(
        OrderLogistics(
            order_id=order.id,
            express_company="顺丰快递",
            express_code="SF",
            logistic_no="SF100200300400",
            status=2,
            trace_json=[
                {"time": "2026-07-01 15:20", "text": "快件已揽收"},
                {"time": "2026-07-02 09:10", "text": "快件正在发往上海转运中心"},
            ],
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
