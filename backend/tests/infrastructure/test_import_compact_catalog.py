from collections import defaultdict

from sqlalchemy import Integer

from app.models import Spu
from scripts.import_compact_catalog import (
    PRODUCTS_PER_BRAND,
    SKUS_PER_PRODUCT,
    TARGET_CATEGORIES,
    build_compact_catalog,
)


def test_compact_catalog_generates_target_scale() -> None:
    products = build_compact_catalog()
    repeated_products = build_compact_catalog()

    assert [product.sales_count for product in repeated_products] == [
        product.sales_count for product in products
    ]

    products_by_category: dict[str, list] = defaultdict(list)
    for product in products:
        products_by_category[product.category].append(product)

    assert set(products_by_category) == set(TARGET_CATEGORIES)
    assert len(products) == len(TARGET_CATEGORIES) * 4 * PRODUCTS_PER_BRAND * SKUS_PER_PRODUCT

    for category_products in products_by_category.values():
        products_by_brand: dict[str, list] = defaultdict(list)
        for product in category_products:
            products_by_brand[product.brand].append(product)

        assert 3 <= len(products_by_brand) <= 5
        for brand_products in products_by_brand.values():
            spu_titles = {product.spu_title for product in brand_products}
            assert 8 <= len(spu_titles) <= 10
            assert len(spu_titles) == PRODUCTS_PER_BRAND

            for spu_title in spu_titles:
                skus = [product for product in brand_products if product.spu_title == spu_title]
                assert 10 <= len(skus) <= 20
                assert len(skus) == SKUS_PER_PRODUCT
                assert len({product.sku_title for product in skus}) == SKUS_PER_PRODUCT
                assert len({product.sales_count for product in skus}) > 1
                assert all(product.sales_count >= 0 for product in skus)

    first_spu = [
        product for product in products if product.spu_title == "Logitech 影刃 M01 游戏鼠标"
    ]
    assert sum(product.sales_count for product in first_spu) == 80 * SKUS_PER_PRODUCT


def test_spu_model_declares_sales_count_column() -> None:
    column = Spu.__table__.c.sales_count

    assert isinstance(column.type, Integer)
    assert not column.nullable
    assert str(column.server_default.arg) == "0"
