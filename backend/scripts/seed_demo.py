import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.models import (
    AppUser,
    AttributeKey,
    AttributeValue,
    Brand,
    Category,
    GoodsAttributeRelation,
    KnowledgeDocument,
    OrderInfo,
    OrderItem,
    OrderLogistics,
    Sku,
    Spu,
    UserAuthCredential,
)
from app.services.auth import PasswordHasher, normalize_login_identifier
from app.services.dataset_mapper import ImportedProduct, attribute_flags, normalize_part_record
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
                sku = await _upsert_product(session, product)
                first_sku = first_sku or sku
        if first_sku:
            await _seed_order(session, user.id, first_sku)
        await seed_user_isolation(session)
        await _seed_knowledge(session)
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


async def _upsert_product(session, product: ImportedProduct) -> Sku:
    category = await _get_or_create_category(session, product.category)
    brand = await _get_or_create_brand(session, product.brand)

    spu = (
        await session.execute(
            select(Spu).where(Spu.title == product.spu_title, Spu.category_id == category.id)
        )
    ).scalar_one_or_none()
    if spu is None:
        spu = Spu(
            category_id=category.id,
            brand_id=brand.id,
            title=product.spu_title,
            sub_title=f"{product.category} 热门款，本地 demo 数据",
            detail_html=f"<p>{product.spu_title}，适合电商客服推荐与参数问答。</p>",
            status=1,
            sales_count=0,
        )
        session.add(spu)
        await session.flush()
    else:
        spu.brand_id = brand.id
        spu.status = 1

    sku = (
        await session.execute(
            select(Sku).where(Sku.spu_id == spu.id, Sku.title == product.sku_title)
        )
    ).scalar_one_or_none()
    if sku is None:
        sku = Sku(
            spu_id=spu.id,
            title=product.sku_title,
            price=product.price_cny,
            stock=product.stock,
            sales_count=max(product.sales_count, 0),
            specs_json=product.specs,
            image_url=None,
            status=1,
        )
        session.add(sku)
    else:
        sku.price = product.price_cny
        sku.stock = max(sku.stock, product.stock)
        sku.sales_count = max(product.sales_count, 0)
        sku.specs_json = product.specs
        sku.status = 1

    await session.flush()

    for attr_name, attr_value in product.attributes.items():
        is_spec, is_filter = attribute_flags(attr_name)
        attr_key = await _get_or_create_attr_key(
            session, category.id, attr_name, is_spec, is_filter
        )
        value = await _get_or_create_attr_value(session, attr_key.id, attr_value)
        exists = (
            await session.execute(
                select(GoodsAttributeRelation.id).where(
                    GoodsAttributeRelation.spu_id == spu.id,
                    GoodsAttributeRelation.sku_id == sku.id,
                    GoodsAttributeRelation.attr_key_id == attr_key.id,
                    GoodsAttributeRelation.attr_value_id == value.id,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                GoodsAttributeRelation(
                    spu_id=spu.id,
                    sku_id=sku.id,
                    attr_key_id=attr_key.id,
                    attr_value_id=value.id,
                )
            )
    return sku


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


async def _get_or_create_category(session, name: str) -> Category:
    category = (
        await session.execute(select(Category).where(Category.name == name))
    ).scalar_one_or_none()
    if category:
        return category
    category = Category(name=name, parent_id=0, level=1)
    session.add(category)
    await session.flush()
    return category


async def _get_or_create_brand(session, name: str) -> Brand:
    brand = (await session.execute(select(Brand).where(Brand.name == name))).scalar_one_or_none()
    if brand:
        return brand
    brand = Brand(name=name)
    session.add(brand)
    await session.flush()
    return brand


async def _get_or_create_attr_key(
    session, category_id: int, name: str, is_spec: bool, is_filter: bool
) -> AttributeKey:
    key = (
        await session.execute(
            select(AttributeKey).where(
                AttributeKey.category_id == category_id, AttributeKey.name == name
            )
        )
    ).scalar_one_or_none()
    if key:
        key.is_spec = key.is_spec or is_spec
        key.is_filter = key.is_filter or is_filter
        return key
    key = AttributeKey(category_id=category_id, name=name, is_spec=is_spec, is_filter=is_filter)
    session.add(key)
    await session.flush()
    return key


async def _get_or_create_attr_value(session, attr_key_id: int, value: str) -> AttributeValue:
    attr_value = (
        await session.execute(
            select(AttributeValue).where(
                AttributeValue.attr_key_id == attr_key_id,
                AttributeValue.value == value,
            )
        )
    ).scalar_one_or_none()
    if attr_value:
        return attr_value
    attr_value = AttributeValue(attr_key_id=attr_key_id, value=value)
    session.add(attr_value)
    await session.flush()
    return attr_value


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


async def _seed_knowledge(session) -> None:
    documents = [
        KnowledgeDocument(
            title="七天无理由退货政策",
            document_type="policy",
            content=(
                "自签收次日起七天内，商品未影响二次销售，可申请七天无理由退货。"
                "人为损坏、缺少配件或包装严重损坏可能影响审核。"
            ),
            metadata_json={"scenario": "return"},
        ),
        KnowledgeDocument(
            title="外设保修说明",
            document_type="policy",
            content=(
                "鼠标、键盘、耳机等外设通常享受一年有限保修。具体以商品页面和品牌官方政策为准。"
            ),
            metadata_json={"scenario": "warranty"},
        ),
        KnowledgeDocument(
            title="发票与发货 FAQ",
            document_type="faq",
            content=(
                "订单支付成功后通常会在二十四小时内安排发货。"
                "电子发票可在订单完成后申请，发票抬头以用户提交信息为准。"
            ),
            metadata_json={"scenario": "invoice_shipping"},
        ),
        KnowledgeDocument(
            title="店铺价保规则",
            document_type="store_rule",
            content=(
                "同一店铺同一 SKU 在签收后七天内出现页面直降，可联系人工客服核验价保。"
                "优惠券、限量秒杀和赠品变化不一定纳入价保范围。"
            ),
            metadata_json={"scenario": "price_protection"},
        ),
        KnowledgeDocument(
            title="机械键盘轴体选购知识",
            document_type="peripheral_knowledge",
            content=(
                "红轴触发压力较轻、声音小，适合办公和游戏；青轴段落感强、声音更清脆，"
                "更适合喜欢明显反馈且不介意噪音的用户。"
            ),
            metadata_json={"category": "键盘"},
        ),
    ]
    existing_titles = set(
        (
            await session.execute(
                select(KnowledgeDocument.title).where(
                    KnowledgeDocument.title.in_([document.title for document in documents])
                )
            )
        )
        .scalars()
        .all()
    )
    session.add_all([document for document in documents if document.title not in existing_titles])


if __name__ == "__main__":
    asyncio.run(main())
