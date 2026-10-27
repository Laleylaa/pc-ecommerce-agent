"""Catalog persistence for synthetic demo initialization."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AttributeKey,
    AttributeValue,
    Brand,
    Category,
    GoodsAttributeRelation,
    Sku,
    Spu,
)
from app.services.dataset_mapper import ImportedProduct, attribute_flags


async def upsert_product(session: AsyncSession, product: ImportedProduct) -> Sku:
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


async def _get_or_create_category(session: AsyncSession, name: str) -> Category:
    category = (
        await session.execute(select(Category).where(Category.name == name))
    ).scalar_one_or_none()
    if category:
        return category
    category = Category(name=name, parent_id=0, level=1)
    session.add(category)
    await session.flush()
    return category


async def _get_or_create_brand(session: AsyncSession, name: str) -> Brand:
    brand = (await session.execute(select(Brand).where(Brand.name == name))).scalar_one_or_none()
    if brand:
        return brand
    brand = Brand(name=name)
    session.add(brand)
    await session.flush()
    return brand


async def _get_or_create_attr_key(
    session: AsyncSession, category_id: int, name: str, is_spec: bool, is_filter: bool
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


async def _get_or_create_attr_value(
    session: AsyncSession, attr_key_id: int, value: str
) -> AttributeValue:
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
