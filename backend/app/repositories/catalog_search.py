"""Read-only catalog access for the stateless search tool."""

from dataclasses import dataclass, field

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
from app.schemas.catalog import ProductCard
from app.schemas.catalog_search import FieldIssue, SearchScope
from app.tools.catalog_fields import canonical_field, same_recorded_value


@dataclass
class CatalogRecord:
    product: ProductCard
    active: bool = True
    issues: list[FieldIssue] = field(default_factory=list)
    description: str = ""


class CatalogSearchRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def load(self, category: str | None, scope: SearchScope) -> list[CatalogRecord]:
        statement = (
            select(Sku, Spu, Brand, Category)
            .join(Spu, Sku.spu_id == Spu.id)
            .join(Brand, Spu.brand_id == Brand.id)
            .join(Category, Spu.category_id == Category.id)
            .order_by(Sku.id)
        )
        if category:
            statement = statement.where(Category.name == category)
        if scope.sku_ids:
            statement = statement.where(Sku.id.in_(scope.sku_ids))
        if scope.spu_ids:
            statement = statement.where(Sku.spu_id.in_(scope.spu_ids))
        rows = (await self.session.execute(statement.limit(10001))).all()
        if len(rows) > 10000:
            raise ValueError("catalog_scope_too_large")
        if not rows:
            return []
        sku_ids = [sku.id for sku, *_ in rows]
        attributes: dict[int, dict[str, list[str]]] = {}
        # Chunk the IN clause without changing the complete result or counts.
        for start in range(0, len(sku_ids), 500):
            statement = (
                select(GoodsAttributeRelation.sku_id, AttributeKey.name, AttributeValue.value)
                .join(AttributeKey, GoodsAttributeRelation.attr_key_id == AttributeKey.id)
                .join(AttributeValue, GoodsAttributeRelation.attr_value_id == AttributeValue.id)
                .where(GoodsAttributeRelation.sku_id.in_(sku_ids[start : start + 500]))
            )
            for sku_id, key, value in (await self.session.execute(statement)).all():
                attributes.setdefault(sku_id, {}).setdefault(canonical_field(key), []).append(value)
        records = []
        for sku, spu, brand, category_row in rows:
            sources: dict[str, list[str]] = {}
            for key, value in (sku.specs_json or {}).items():
                if value is not None and str(value).strip():
                    sources.setdefault(canonical_field(str(key)), []).append(str(value))
            for key, values in attributes.get(sku.id, {}).items():
                sources.setdefault(key, []).extend(values)
            specs, issues = {}, []
            for key, values in sources.items():
                distinct = []
                for value in values:
                    if not any(
                        same_recorded_value(key, value, previous) for previous in distinct
                    ):
                        distinct.append(value)
                if len(distinct) > 1:
                    issues.append(
                        FieldIssue(sku_id=sku.id, field=key, status="conflict", values=distinct)
                    )
                if distinct:
                    specs[key] = distinct[0]
            if "无线版" in sku.title and specs.get("wireless") == "否":
                issues.extend(
                    FieldIssue(
                        sku_id=sku.id,
                        field=key,
                        status="conflict",
                        values=["标题：无线版", f"规格：{specs.get(key, '')}"],
                    )
                    for key in ("wireless", "connection_type")
                )
            records.append(
                CatalogRecord(
                    product=ProductCard(
                        spu_id=spu.id,
                        sku_id=sku.id,
                        title=sku.title,
                        spu_title=spu.title,
                        brand=brand.name,
                        category=category_row.name,
                        price=sku.price,
                        stock=sku.stock,
                        sku_sales_count=sku.sales_count,
                        sales_count=spu.sales_count,
                        specs=specs,
                        image_url=sku.image_url,
                    ),
                    active=sku.status == 1 and spu.status == 1,
                    issues=issues,
                    description=spu.detail_html or "",
                )
            )
        return records
