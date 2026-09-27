import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from random import Random

from app.core.database import AsyncSessionLocal
from app.services.dataset_mapper import ImportedProduct
from scripts.seed_demo import _upsert_product

TARGET_CATEGORIES = ("鼠标", "键盘", "耳机", "显示器", "音箱", "摄像头")
PRODUCTS_PER_BRAND = 8
SKUS_PER_PRODUCT = 12
SKU_SALES_RANDOM_SEED = 20_260_724

VARIANT_LABELS = (
    "黑色标准版",
    "白色标准版",
    "银色增强版",
    "粉色增强版",
    "黑色无线版",
    "白色无线版",
    "银色旗舰版",
    "粉色旗舰版",
    "黑色套装版",
    "白色套装版",
    "银色电竞版",
    "粉色电竞版",
)


@dataclass(frozen=True)
class CategoryTemplate:
    category: str
    brands: tuple[str, str, str, str]
    product_lines: tuple[str, ...]
    model_prefix: str
    product_suffix: str
    base_price: Decimal
    price_step: Decimal


CATEGORY_TEMPLATES = (
    CategoryTemplate(
        category="鼠标",
        brands=("Logitech", "Razer", "SteelSeries", "Pulsar"),
        product_lines=("影刃", "星环", "疾风", "幻翼", "极光", "追光", "雷霆", "云梭"),
        model_prefix="M",
        product_suffix="游戏鼠标",
        base_price=Decimal("129.00"),
        price_step=Decimal("9.00"),
    ),
    CategoryTemplate(
        category="键盘",
        brands=("Keychron", "Razer", "Akko", "Wooting"),
        product_lines=("星轴", "银翼", "山岚", "极夜", "流光", "云顶", "青锋", "曜石"),
        model_prefix="K",
        product_suffix="机械键盘",
        base_price=Decimal("199.00"),
        price_step=Decimal("13.00"),
    ),
    CategoryTemplate(
        category="耳机",
        brands=("Logitech", "SteelSeries", "HyperX", "Sony"),
        product_lines=("声场", "听风", "猎音", "星麦", "云声", "极境", "回响", "曜音"),
        model_prefix="H",
        product_suffix="游戏耳机",
        base_price=Decimal("159.00"),
        price_step=Decimal("11.00"),
    ),
    CategoryTemplate(
        category="显示器",
        brands=("Dell", "LG", "ASUS", "AOC"),
        product_lines=("锐屏", "广域", "星幕", "竞速", "明眸", "云视", "曜屏", "灵境"),
        model_prefix="D",
        product_suffix="显示器",
        base_price=Decimal("699.00"),
        price_step=Decimal("45.00"),
    ),
    CategoryTemplate(
        category="音箱",
        brands=("Edifier", "JBL", "Creative", "Bose"),
        product_lines=("声塔", "环幕", "清音", "低频", "桌面", "流声", "星环", "曜声"),
        model_prefix="S",
        product_suffix="桌面音箱",
        base_price=Decimal("129.00"),
        price_step=Decimal("15.00"),
    ),
    CategoryTemplate(
        category="摄像头",
        brands=("Logitech", "Razer", "Elgato", "AVerMedia"),
        product_lines=("清眸", "锐影", "星视", "直播", "云台", "广角", "曜影", "灵犀"),
        model_prefix="W",
        product_suffix="高清摄像头",
        base_price=Decimal("149.00"),
        price_step=Decimal("12.00"),
    ),
)


def build_compact_catalog() -> list[ImportedProduct]:
    products: list[ImportedProduct] = []
    for category_index, template in enumerate(CATEGORY_TEMPLATES):
        for brand_index, brand in enumerate(template.brands):
            for product_index, line in enumerate(template.product_lines[:PRODUCTS_PER_BRAND]):
                spu_title = _spu_title(template, brand, line, product_index)
                sales_counts = _sku_sales_counts(
                    category_index,
                    brand_index,
                    product_index,
                )
                for sku_index, sales_count in enumerate(sales_counts):
                    attributes = _attributes_for_category(template.category, sku_index)
                    sku_title = f"{spu_title} {VARIANT_LABELS[sku_index]}"
                    products.append(
                        ImportedProduct(
                            category=template.category,
                            brand=brand,
                            spu_title=spu_title,
                            sku_title=sku_title,
                            price_cny=_price(template, brand_index, product_index, sku_index),
                            stock=_stock(category_index, brand_index, product_index, sku_index),
                            sales_count=sales_count,
                            specs=attributes,
                            attributes=attributes,
                        )
                    )
    return products


async def import_compact_catalog(limit: int | None = None) -> int:
    products = build_compact_catalog()
    if limit is not None:
        products = products[:limit]

    async with AsyncSessionLocal() as session:
        for product in products:
            await _upsert_product(session, product)
        await session.commit()
    return len(products)


def summarize_catalog(products: list[ImportedProduct]) -> dict[str, int]:
    categories = {product.category for product in products}
    category_brand_pairs = {(product.category, product.brand) for product in products}
    spus = {(product.category, product.brand, product.spu_title) for product in products}
    return {
        "categories": len(categories),
        "category_brand_pairs": len(category_brand_pairs),
        "spus": len(spus),
        "skus": len(products),
    }


def _spu_title(
    template: CategoryTemplate,
    brand: str,
    line: str,
    product_index: int,
) -> str:
    model = f"{template.model_prefix}{product_index + 1:02d}"
    return f"{brand} {line} {model} {template.product_suffix}"


def _price(
    template: CategoryTemplate,
    brand_index: int,
    product_index: int,
    sku_index: int,
) -> Decimal:
    return (
        template.base_price
        + Decimal(brand_index * 20)
        + Decimal(product_index) * template.price_step
        + Decimal(sku_index * 5)
    ).quantize(Decimal("0.01"))


def _stock(
    category_index: int,
    brand_index: int,
    product_index: int,
    sku_index: int,
) -> int:
    return 20 + ((category_index * 17 + brand_index * 11 + product_index * 7 + sku_index * 5) % 180)


def _sku_sales_counts(
    category_index: int,
    brand_index: int,
    product_index: int,
) -> tuple[int, ...]:
    average_sales_count = 80 + category_index * 320 + brand_index * 73 + product_index * 19
    spu_sales_total = average_sales_count * SKUS_PER_PRODUCT
    randomizer = Random(
        SKU_SALES_RANDOM_SEED
        + category_index * 1_000_000
        + brand_index * 10_000
        + product_index * 100
    )
    weights = [randomizer.randint(50, 150) for _ in range(SKUS_PER_PRODUCT)]
    weight_total = sum(weights)
    weighted_sales = [spu_sales_total * weight for weight in weights]
    sales_counts = [value // weight_total for value in weighted_sales]

    remainder = spu_sales_total - sum(sales_counts)
    remainder_order = sorted(
        range(SKUS_PER_PRODUCT),
        key=lambda sku_index: (
            weighted_sales[sku_index] % weight_total,
            -sku_index,
        ),
        reverse=True,
    )
    for sku_index in remainder_order[:remainder]:
        sales_counts[sku_index] += 1

    return tuple(sales_counts)


def _attributes_for_category(category: str, sku_index: int) -> dict[str, str]:
    color = ("黑色", "白色", "银色", "粉色")[sku_index % 4]
    connection = ("有线", "蓝牙", "2.4G 无线", "三模")[sku_index % 4]
    wireless = "否" if connection == "有线" else "是"

    if category == "鼠标":
        return {
            "tracking_method": "光学",
            "connection_type": connection,
            "wireless": wireless,
            "max_dpi": str(16000 + (sku_index % 6) * 4000),
            "hand_orientation": "右手",
            "color": color,
            "weight_g": str(58 + sku_index % 8),
        }
    if category == "键盘":
        return {
            "style": ("60%", "75%", "87 键", "104 键")[sku_index % 4],
            "switches": ("线性红轴", "段落茶轴", "静音红轴", "磁轴")[sku_index % 4],
            "backlit": ("白光", "RGB", "无背光", "RGB")[sku_index % 4],
            "tenkeyless": "否" if sku_index % 4 == 3 else "是",
            "connection_type": connection,
            "wireless": wireless,
            "color": color,
        }
    if category == "耳机":
        return {
            "type": ("头戴式", "入耳式")[sku_index % 2],
            "microphone": "是" if sku_index % 3 != 0 else "否",
            "wireless": wireless,
            "enclosure_type": ("封闭式", "开放式")[sku_index % 2],
            "frequency_response": ("20Hz-20kHz", "10Hz-40kHz")[sku_index % 2],
            "connection_type": connection,
            "color": color,
        }
    if category == "显示器":
        return {
            "size_inch": ("24", "27", "32", "34")[sku_index % 4],
            "resolution": ("1920x1080", "2560x1440", "3840x2160", "3440x1440")[sku_index % 4],
            "refresh_rate": ("75Hz", "144Hz", "165Hz", "240Hz")[sku_index % 4],
            "panel_type": ("IPS", "VA", "OLED", "Fast IPS")[sku_index % 4],
            "response_time_ms": ("1", "3", "5")[sku_index % 3],
            "color": color,
        }
    if category == "音箱":
        return {
            "type": ("2.0", "2.1", "Soundbar", "监听音箱")[sku_index % 4],
            "connection_type": connection,
            "wireless": wireless,
            "channels": ("2.0", "2.1", "3.1", "5.1")[sku_index % 4],
            "power_w": str(20 + (sku_index % 6) * 10),
            "color": color,
        }
    if category == "摄像头":
        return {
            "resolution": ("1080p", "1440p", "4K", "1080p HDR")[sku_index % 4],
            "frame_rate": ("30fps", "60fps", "90fps")[sku_index % 3],
            "microphone": "是" if sku_index % 4 != 0 else "否",
            "connection_type": "USB-C" if sku_index % 2 else "USB-A",
            "field_of_view": ("78°", "90°", "103°")[sku_index % 3],
            "color": color,
        }
    raise ValueError(f"Unsupported category: {category}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Maximum SKUs to import")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated catalog summary without writing the database",
    )
    args = parser.parse_args()

    if args.dry_run:
        summary = summarize_catalog(build_compact_catalog())
        print(
            "Generated compact catalog: "
            f"{summary['categories']} categories, "
            f"{summary['category_brand_pairs']} category-brand pairs, "
            f"{summary['spus']} SPUs, "
            f"{summary['skus']} SKUs"
        )
        return

    count = await import_compact_catalog(limit=args.limit)
    print(f"Imported {count} compact catalog SKUs")


if __name__ == "__main__":
    asyncio.run(main())
