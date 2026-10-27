"""Fresh-database demo initialization. Never reset or overwrite an existing installation."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base
from scripts.demo.accounts import create_accounts
from scripts.demo.catalog import build_compact_catalog, summarize_catalog
from scripts.demo.catalog_store import upsert_product
from scripts.demo.orders import create_orders
from scripts.demo.policies import load_policies

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "data" / "demo" / "manifest.json"


class DemoDatabaseNotEmpty(ValueError):
    pass


async def require_empty_database(session: AsyncSession) -> None:
    # Serialize demo loaders; initialization must run before starting application writers.
    await session.execute(text("SELECT pg_advisory_xact_lock(7242026)"))
    for table in Base.metadata.sorted_tables:
        if (await session.execute(select(table).limit(1))).first() is not None:
            raise DemoDatabaseNotEmpty(
                f"Database already contains {table.name} records; no demo data was changed. "
                "Reuse the existing installation or configure a separate empty database."
            )


async def load_demo(session: AsyncSession, anchor: datetime) -> dict:
    await require_empty_database(session)
    skus = [await upsert_product(session, product) for product in build_compact_catalog()]
    users = await create_accounts(session)
    orders = await create_orders(session, users, skus, anchor)
    documents = await load_policies(session)
    return {"users": len(users), "skus": len(skus), "orders": orders, "documents": documents}


async def initialize() -> dict:
    # Summary/help never import database settings or construct an engine.
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session, session.begin():
        return await load_demo(session, datetime.now(UTC).replace(tzinfo=None))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", action="store_true", help="Read inputs only; no database")
    args = parser.parse_args()
    if args.summary:
        print(json.dumps({
            "manifest": json.loads(MANIFEST_PATH.read_text()),
            "catalog": summarize_catalog(build_compact_catalog()),
        }, ensure_ascii=False, indent=2))
        return
    try:
        result = asyncio.run(initialize())
    except DemoDatabaseNotEmpty as exc:
        raise SystemExit(str(exc)) from None
    print("Demo initialized: " + json.dumps(result))


if __name__ == "__main__":
    main()
