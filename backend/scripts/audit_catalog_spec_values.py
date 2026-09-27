
import argparse
import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.models import (
    AttributeKey,
    AttributeValue,
    Brand,
    Category,
    GoodsAttributeRelation,
    Sku,
    Spu,
)

DEFAULT_OUTPUT = Path("backend/data/catalog_spec_values_audit.json")


def _empty_stat() -> dict[str, Any]:
    return {
        "count": 0,
        "sku_count": 0,
        "categories": set(),
        "brands": set(),
        "sources": set(),
    }


def _record(
    stats: dict[str, dict[str, dict[str, Any]]],
    *,
    key: str,
    value: str,
    category: str,
    brand: str,
    sku_count: int,
    source: str,
) -> None:
    key = str(key).strip()
    value = str(value).strip()
    if not key or not value:
        return
    stat = stats[key].setdefault(value, _empty_stat())
    stat["count"] += 1
    stat["sku_count"] += sku_count
    stat["categories"].add(str(category))
    stat["brands"].add(str(brand))
    stat["sources"].add(source)


def _finalize_stats(
    stats: dict[str, dict[str, dict[str, Any]]], *, top_values: int
) -> dict[str, Any]:
    finalized: dict[str, Any] = {}
    for key in sorted(stats):
        values = []
        for value, stat in stats[key].items():
            values.append(
                {
                    "value": value,
                    "count": stat["count"],
                    "sku_count": stat["sku_count"],
                    "categories": sorted(stat["categories"]),
                    "brands": sorted(stat["brands"]),
                    "sources": sorted(stat["sources"]),
                }
            )
        values.sort(key=lambda item: (-item["sku_count"], -item["count"], item["value"]))
        finalized[key] = {
            "distinct_value_count": len(values),
            "values": values[:top_values],
        }
    return finalized


async def collect_catalog_spec_values(*, top_values: int = 50) -> dict[str, Any]:
    stats: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    async with AsyncSessionLocal() as session:
        sku_rows = (
            await session.execute(
                select(Sku.specs_json, Category.name, Brand.name)
                .join(Spu, Sku.spu_id == Spu.id)
                .join(Category, Spu.category_id == Category.id)
                .join(Brand, Spu.brand_id == Brand.id)
                .where(Sku.status == 1, Spu.status == 1, Sku.specs_json.is_not(None))
            )
        ).all()
        for specs_json, category, brand in sku_rows:
            if not isinstance(specs_json, dict):
                continue
            for key, value in specs_json.items():
                _record(
                    stats,
                    key=str(key),
                    value=str(value),
                    category=str(category),
                    brand=str(brand),
                    sku_count=1,
                    source="sku.specs_json",
                )

        attr_rows = (
            await session.execute(
                select(
                    AttributeKey.name,
                    AttributeValue.value,
                    Category.name,
                    Brand.name,
                    func.count(func.distinct(GoodsAttributeRelation.sku_id)),
                )
                .join(
                    AttributeValue,
                    GoodsAttributeRelation.attr_value_id == AttributeValue.id,
                )
                .join(AttributeKey, GoodsAttributeRelation.attr_key_id == AttributeKey.id)
                .join(Spu, GoodsAttributeRelation.spu_id == Spu.id)
                .join(Category, Spu.category_id == Category.id)
                .join(Brand, Spu.brand_id == Brand.id)
                .where(Spu.status == 1)
                .group_by(AttributeKey.name, AttributeValue.value, Category.name, Brand.name)
            )
        ).all()
        for key, value, category, brand, sku_count in attr_rows:
            _record(
                stats,
                key=str(key),
                value=str(value),
                category=str(category),
                brand=str(brand),
                sku_count=int(sku_count or 0),
                source="attribute_value",
            )

        summary_rows = (
            await session.execute(
                select(
                    func.count(func.distinct(Category.id)),
                    func.count(func.distinct(Brand.id)),
                    func.count(func.distinct(Spu.id)),
                    func.count(func.distinct(Sku.id)),
                )
                .select_from(Sku)
                .join(Spu, Sku.spu_id == Spu.id)
                .join(Category, Spu.category_id == Category.id)
                .join(Brand, Spu.brand_id == Brand.id)
                .where(Sku.status == 1, Spu.status == 1)
            )
        ).one()

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "category_count": int(summary_rows[0] or 0),
            "brand_count": int(summary_rows[1] or 0),
            "spu_count": int(summary_rows[2] or 0),
            "sku_count": int(summary_rows[3] or 0),
            "spec_key_count": len(stats),
        },
        "specs": _finalize_stats(stats, top_values=top_values),
    }


def write_markdown_report(audit: dict[str, Any], output: Path) -> None:
    lines = [
        "# Catalog Spec Values Audit",
        "",
        f"Generated at: `{audit['generated_at']}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in audit["summary"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Values", ""])
    for spec_key, payload in audit["specs"].items():
        lines.append(f"### `{spec_key}`")
        lines.append("")
        lines.append(f"Distinct values: `{payload['distinct_value_count']}`")
        lines.append("")
        lines.append("| Value | SKU Count | Categories | Sources |")
        lines.append("| --- | ---: | --- | --- |")
        for item in payload["values"]:
            categories = ", ".join(item["categories"])
            sources = ", ".join(item["sources"])
            value = str(item["value"]).replace("|", "\\|")
            lines.append(f"| `{value}` | {item['sku_count']} | {categories} | {sources} |")
        lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit distinct catalog spec values for tool alias maintenance."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--top-values", type=int, default=50)
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    audit = await collect_catalog_spec_values(top_values=args.top_values)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        write_markdown_report(audit, args.markdown_output)
    print(
        f"Wrote catalog spec audit to {args.output} "
        f"({audit['summary']['spec_key_count']} spec keys)."
    )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
