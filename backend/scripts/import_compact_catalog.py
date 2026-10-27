"""Explicit catalog maintenance command; fresh setup uses scripts.demo.load."""

import argparse
import asyncio

from app.core.database import AsyncSessionLocal

from scripts.demo.catalog import build_compact_catalog, summarize_catalog
from scripts.demo.catalog_store import upsert_product


async def import_compact_catalog(limit: int | None = None) -> int:
    products = build_compact_catalog()
    if limit is not None:
        products = products[:limit]
    async with AsyncSessionLocal() as session:
        for product in products:
            await upsert_product(session, product)
        await session.commit()
    return len(products)


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
