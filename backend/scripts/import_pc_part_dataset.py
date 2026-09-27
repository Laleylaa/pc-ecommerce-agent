import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.database import AsyncSessionLocal
from app.services.dataset_mapper import normalize_part_record
from scripts.seed_demo import _upsert_product

DEFAULT_PART_TYPES = ("headphones", "keyboard", "mouse")


@dataclass(frozen=True)
class ImportTarget:
    path: Path
    part_type: str


def infer_part_type(path: str | Path) -> str:
    return Path(path).name.split(".", 1)[0]


def load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{path} must contain a JSON array or JSON Lines records")
    return records


def resolve_import_targets(path: Path, part_types: list[str] | None) -> list[ImportTarget]:
    if path.is_file():
        part_type = part_types[0] if part_types else infer_part_type(path)
        return [ImportTarget(path=path, part_type=part_type)]

    selected = set(part_types or DEFAULT_PART_TYPES)
    targets = [
        ImportTarget(path=item, part_type=infer_part_type(item))
        for item in sorted(path.iterdir())
        if item.suffix in {".json", ".jsonl"} and infer_part_type(item) in selected
    ]
    if not targets:
        raise ValueError(f"No matching dataset files found in {path}")
    return targets


async def import_file(path: Path, part_type: str, limit: int | None) -> int:
    records = load_records(path)
    imported = 0
    async with AsyncSessionLocal() as session:
        for record in records:
            product = normalize_part_record(part_type, record)
            if product is None:
                continue
            await _upsert_product(session, product)
            imported += 1
            if limit and imported >= limit:
                break
        await session.commit()
    return imported


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        type=Path,
        help="Path to a pc-part-dataset JSON/JSONL file or data/json directory",
    )
    parser.add_argument(
        "--part-type",
        help="Dataset part type for a single file, e.g. mouse or keyboard",
    )
    parser.add_argument(
        "--part-types",
        help="Comma-separated part types for a directory. Defaults to headphones,keyboard,mouse",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum records to import per file",
    )
    args = parser.parse_args()
    part_types = (
        [item.strip() for item in args.part_types.split(",") if item.strip()]
        if args.part_types
        else ([args.part_type] if args.part_type else None)
    )
    total = 0
    for target in resolve_import_targets(args.path, part_types):
        imported = await import_file(target.path, target.part_type, args.limit)
        total += imported
        print(f"Imported {imported} {target.part_type} products from {target.path}")
    print(f"Imported {total} products total")


if __name__ == "__main__":
    asyncio.run(main())
