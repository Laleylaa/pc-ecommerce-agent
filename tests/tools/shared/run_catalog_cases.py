"""Shared explicit runner for three independently selected catalog tools; never pytest-collected.

Only --live --prepare/--run touches external services. Uses the formal
binding and trace/budget lifecycle; expected facts come from independent SELECTs.
"""

import argparse
import asyncio
import copy
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from catalog_basis import read_basis
from catalog_evidence import (
    HERE,
    RESULTS,
    ROOT,
    TOOLS,
    batch_path,
    code_state,
    now,
    read_json,
    run_cases,
    sha,
    write_json,
)

GROUPS = ("lookup", "search", "facets")


def script_state() -> dict:
    return {p.name: sha(p) for p in sorted(HERE.glob("*.py"))}


def resolve(value, oracle: dict, bindings: dict):
    """Resolve only predeclared oracle references, before the tool is called."""
    if isinstance(value, dict):
        return {key: resolve(item, oracle, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, oracle, bindings) for item in value]
    if isinstance(value, str):
        if value.startswith("$oracle."):
            return oracle[value.removeprefix("$oracle.")]
        if "{{product_name}}" in value:
            value = value.replace("{{product_name}}", oracle["product_name"])
        return re.sub(
            r"\{\{(sku_id|spu_id)\}\}", lambda match: str(bindings[match[1]]), value
        )
    return value


async def live_main(group: str, args: argparse.Namespace) -> None:
    if not args.live or os.environ.get("PC_AGENT_OFFLINE_TEST"):
        raise SystemExit(
            "Explicit --live required, outside tests/run.py; offline guards stay on"
        )
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]
    from app.core.config import get_settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        connect_args={
            "server_settings": {
                "default_transaction_read_only": "on",
                "statement_timeout": "15000",
            }
        },
    )
    try:
        async with AsyncSession(engine) as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            transaction = {
                "read_only": (
                    await session.execute(text("SHOW transaction_read_only"))
                ).scalar_one(),
                "isolation": (
                    await session.execute(text("SHOW transaction_isolation"))
                ).scalar_one(),
                "database_time": str(
                    (
                        await session.execute(text("SELECT current_timestamp"))
                    ).scalar_one()
                ),
            }
            if args.prepare:
                template = read_json(TOOLS / f"catalog_{group}" / "cases.json")
                if args.kind:
                    template["cases"] = [
                        c for c in template["cases"] if c["kind"] == args.kind
                    ]
                if args.case:
                    template["cases"] = [c for c in template["cases"] if c["id"] == args.case]
                if not template["cases"]:
                    raise SystemExit(
                        "No case matches the selected tool/kind/case; nothing prepared"
                    )
                bindings = read_json(Path(args.bindings)) if args.bindings else {}
                if not isinstance(bindings, dict) or set(bindings) - {
                    c["id"] for c in template["cases"]
                }:
                    raise SystemExit(
                        "Bindings must map this suite's case IDs to sourced IDs"
                    )
                if any(
                    not isinstance(value, dict)
                    or set(value) - {"sku_id", "spu_id", "source"}
                    for value in bindings.values()
                ):
                    raise SystemExit(
                        "Each binding accepts only sku_id/spu_id and source"
                    )
                name = (
                    datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                    + "-"
                    + group
                    + "-"
                    + uuid4().hex[:6]
                )
                folder = RESULTS / name
                (folder / "evidence/traces").mkdir(parents=True, exist_ok=False)
                (folder / "checks").mkdir()
                prepared, suite = {}, copy.deepcopy(template)
                for case in suite["cases"]:
                    basis = await read_basis(
                        session, case, bindings.get(case["id"], {})
                    )
                    prepared[case["id"]] = basis
                    case["readiness"] = {
                        "eligible": basis["eligible"],
                        "reason": basis["reason"],
                    }
                    if basis["eligible"]:
                        # Template and raw data remain next to the resolved expected values.
                        case["input"] = resolve(
                            case["input"], basis["oracle"], basis["parameters"]
                        )
                        case["hard"] = resolve(
                            case["hard"], basis["oracle"], basis["parameters"]
                        )
                        case["review_reference"] = resolve(
                            case.get("review_reference", []), basis["oracle"], basis["parameters"]
                        )
                        if "allowed_identity_brand" in case:
                            case["allowed_identity_brand"] = resolve(
                                case["allowed_identity_brand"],
                                basis["oracle"],
                                basis["parameters"],
                            )
                data = folder / "evidence"
                write_json(data / "case_templates.json", template)
                write_json(data / "cases.json", suite)
                write_json(
                    data / "baseline.json",
                    {"prepared": prepared, "transaction": transaction},
                )
                write_json(
                    data / "environment.json",
                    {
                        "layout_version": 2,
                        "batch_id": name,
                        "group": group,
                        "created_at": now(),
                        "code": code_state(),
                        "scripts": script_state(),
                        "bindings": bindings,
                        "model": settings.llm_model,
                        "provider": settings.llm_provider,
                        "temperature": 0,
                        "case_version": suite["version"],
                        "frozen_sha256": {
                            file: sha(data / file)
                            for file in (
                                "cases.json",
                                "case_templates.json",
                                "baseline.json",
                            )
                        },
                        "database": (
                            "configured catalog; read-only transaction; credentials omitted"
                        ),
                        "execution": "formal single-tool binding; no agent/chat/frontend",
                    },
                )
                print(name)
                return

            folder = batch_path(args.run)
            data = folder / "evidence"
            manifest = read_json(data / "environment.json")
            if manifest["group"] != group or manifest.get("execution_started"):
                raise SystemExit(
                    "Wrong suite or batch already attempted; prepare a new batch"
                )
            if any(
                sha(data / file) != digest
                for file, digest in manifest["frozen_sha256"].items()
            ):
                raise SystemExit("Frozen input/baseline changed; no tool calls made")
            if (
                code_state()["fingerprints"] != manifest["code"]["fingerprints"]
                or script_state() != manifest["scripts"]
            ):
                raise SystemExit(
                    "Source/scripts changed since preparation; prepare a new batch"
                )
            if (
                settings.llm_model != manifest["model"]
                or settings.llm_provider != manifest["provider"]
            ):
                raise SystemExit(
                    "Model/provider changed since preparation; prepare a new batch"
                )
            suite = read_json(data / "cases.json")
            prepared = read_json(data / "baseline.json")["prepared"]
            eligible, before, skipped = [], {}, {}
            for case in suite["cases"]:
                prior = prepared[case["id"]]
                if not prior["eligible"]:
                    skipped[case["id"]] = prior["reason"]
                    continue
                current = await read_basis(
                    session, case, manifest["bindings"].get(case["id"], {})
                )
                before[case["id"]] = current
                if not current["eligible"] or current.get("oracle") != prior["oracle"]:
                    skipped[case["id"]] = "data_changed_or_unverifiable"
                else:
                    eligible.append(case)
            # Do not mutate the original frozen baseline, even on a data-change skip.
            write_json(
                data / "preflight.json",
                {
                    "before_execution": before,
                    "transaction": transaction,
                    "skipped": skipped,
                },
            )
            manifest["execution_started"] = {
                "at": now(),
                "skipped": skipped,
                "eligible": [c["id"] for c in eligible],
            }
            write_json(data / "environment.json", manifest)
            if eligible and (
                not settings.catalog_llm_planner_enabled or not settings.llm_api_key
            ):
                manifest["execution_error"] = "real_planning_not_configured"
                write_json(data / "environment.json", manifest)
                raise SystemExit("Real planning must be configured; batch not reusable")
            try:
                if eligible:
                    manifest["export"] = await run_cases(
                        session, settings, folder, suite | {"cases": eligible}
                    )
            except Exception as exc:
                manifest["execution_error"] = type(exc).__name__
                raise
            finally:
                after = {}
                async with AsyncSession(engine) as verification:
                    await verification.execute(
                        text(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                        )
                    )
                    for case in eligible:
                        after[case["id"]] = await read_basis(verification, case, {})
                write_json(data / "postflight.json", {"after_execution": after})
                manifest["code_after"] = code_state()
                manifest["execution_finished_at"] = now()
                write_json(data / "environment.json", manifest)
            print(f"Completed attempts: {len(eligible)}; not executed: {len(skipped)}")
    finally:
        await engine.dispose()


def cli(group: str) -> None:
    if group not in GROUPS:
        raise ValueError(group)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--bindings", help="Reserved sourced bindings; first suite needs none"
    )
    parser.add_argument(
        "--kind",
        choices=("smoke", "contract", "business", "boundary"),
        help="Prepare only one predeclared purpose; omit for all cases in this tool",
    )
    parser.add_argument("--case", help="Prepare only this exact case ID")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--run", metavar="BATCH")
    args = parser.parse_args()
    if args.case and not args.prepare:
        parser.error("--case is only accepted during --prepare")
    if args.kind and not args.prepare:
        parser.error("--kind is only accepted during --prepare")
    if args.bindings and not args.prepare:
        parser.error("--bindings is only accepted during --prepare")
    asyncio.run(live_main(group, args))


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in GROUPS:
        raise SystemExit("Usage: run_catalog_cases.py lookup|search|facets [options]")
    selected = sys.argv.pop(1)
    cli(selected)
