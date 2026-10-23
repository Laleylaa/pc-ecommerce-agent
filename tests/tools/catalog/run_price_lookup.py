"""Explicit live price-lookup pilot; not a pytest module. No import-time I/O.

From repository root:
  backend/.venv/bin/python tests/tools/catalog/run_price_lookup.py --live --prepare
  backend/.venv/bin/python tests/tools/catalog/run_price_lookup.py --live --run BATCH
Then use check_price_lookup.py --fetch BATCH and --render BATCH after manual review.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SQL = """SELECT p.id AS spu_id, p.title AS spu_title, b.name AS brand,
 c.name AS category, s.id AS sku_id, s.title AS sku_title, s.price,
 p.status AS spu_status, s.status AS sku_status
 FROM spu p JOIN sku s ON s.spu_id=p.id
 JOIN brand b ON b.id=p.brand_id JOIN category c ON c.id=p.category_id
 WHERE p.id=33 ORDER BY s.id"""


def now():
    return datetime.now(UTC).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    return json.loads(path.read_text())


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if (
        path.exists()) else []


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_state():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    files = list((ROOT / "backend/app").rglob("*.py"))
    files += [ROOT / "tests/run.py", ROOT / "pytest.ini"]
    return {"head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
            "worktree": git("status", "--short"),
            "fingerprints": {str(p.relative_to(ROOT)): sha(p) for p in sorted(files)}}


def batch_path(name):
    path = (HERE / "results" / name).resolve()
    if path.parent != (HERE / "results").resolve() or not path.is_dir():
        raise ValueError("Batch must be an existing direct child of catalog/results")
    return path


async def basis(session):
    from sqlalchemy import text
    timestamp = (await session.execute(text("SELECT current_timestamp"))).scalar_one()
    rows = [dict(r) for r in (await session.execute(text(SQL))).mappings()]
    for row in rows:
        row["price"] = str(row["price"])
    return {"checked_at": str(timestamp), "sql": SQL, "rows": rows,
            "read_only": (await session.execute(text("SHOW transaction_read_only"))).scalar_one(),
            "isolation": (await session.execute(text("SHOW transaction_isolation"))).scalar_one()}


def verify_basis(suite, evidence):
    from decimal import Decimal
    rows = evidence["rows"]
    checks = []
    for case in suite["cases"]:
        ids = case["population"]["sku_ids"]
        selected = [r for r in rows if r["sku_id"] in ids]
        active = [r for r in rows if r["sku_status"] == r["spu_status"] == 1]
        actual_ids = [r["sku_id"] for r in active] if case["population"]["kind"] == "spu" else [
            r["sku_id"] for r in active if r["sku_id"] == case["population"]["id"]]
        expected = {p: v for p, _, v in case["hard"]}
        valid = (
            actual_ids == ids and len(selected) == expected["output.facts.sku_count"]
            and all(r["spu_id"] == 33 and r["brand"] == "Keychron"
                    and r["spu_title"] == "Keychron 星轴 K01 机械键盘" for r in selected)
            and (not case.get("expected_sku_title") or all(
                r["sku_title"] == case["expected_sku_title"] for r in selected))
            and min(Decimal(r["price"]) for r in selected)
            == Decimal(expected["output.facts.min_price"])
            and max(Decimal(r["price"]) for r in selected)
            == Decimal(expected["output.facts.max_price"])
        )
        checks.append({"case_id": case["id"], "valid": valid, "actual_sku_ids": actual_ids})
    return checks


async def main(args):
    # Never undo the offline runner's protections or load credentials during collection.
    if not args.live or os.environ.get("PC_AGENT_OFFLINE_TEST"):
        raise SystemExit("Explicit --live required, outside tests/run.py")
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]
    from app.core.config import get_settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    settings = get_settings()
    engine = create_async_engine(settings.database_url, connect_args={
        "server_settings": {"default_transaction_read_only": "on"}})
    try:
        async with AsyncSession(engine) as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            evidence = await basis(session)
            if args.prepare:
                name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-price-" + uuid4().hex[:6]
                folder = HERE / "results" / name
                folder.mkdir(parents=True, exist_ok=False)
                data_dir = folder / "evidence"
                (data_dir / "traces").mkdir(parents=True)
                (folder / "checks").mkdir()
                suite = read_json(HERE / "price_lookup_cases.json")
                write_json(data_dir / "cases.json", suite)
                write_json(data_dir / "baseline.json", {
                    "prepared": evidence, "prepare_checks": verify_basis(suite, evidence)})
                write_json(data_dir / "environment.json", {
                    "layout_version": 2,
                    "batch_id": name, "created_at": now(), "code": code_state(),
                    "model": settings.llm_model, "provider": settings.llm_provider,
                    "temperature": 0, "case_version": suite["version"],
                    "cases_sha256": sha(data_dir / "cases.json"),
                    "scripts": {p.name: sha(p) for p in HERE.glob("*price_lookup.py")},
                    "database": "configured local catalog; credentials omitted; SELECT only",
                    "execution": "formal single-tool binding; no loop/chat/API/answer/frontend",
                })
                print(name)
                return
            folder = batch_path(args.run)
            data_dir = folder / "evidence"
            manifest = read_json(data_dir / "environment.json")
            if manifest.get("execution_started"):
                raise SystemExit("Batch already attempted; prepare a new batch, never overwrite")
            suite = read_json(data_dir / "cases.json")
            baseline = read_json(data_dir / "baseline.json")
            baseline["before_execution"] = evidence
            write_json(data_dir / "baseline.json", baseline)
            checks = verify_basis(suite, evidence)
            reason = None
            if sha(data_dir / "cases.json") != manifest["cases_sha256"]:
                reason = "case_snapshot_changed"
            elif evidence["rows"] != baseline["prepared"]["rows"]:
                reason = "data_changed"
            elif not all(c["valid"] for c in checks):
                reason = "independent_expectation_mismatch"
            elif code_state()["fingerprints"] != manifest["code"]["fingerprints"]:
                reason = "source_changed"
            manifest["execution_started"] = {
                "at": now(), "preflight_reason": reason, "basis_checks": checks}
            write_json(data_dir / "environment.json", manifest)
            if reason:
                raise SystemExit(reason + "; no tool calls made")
            if not settings.catalog_llm_planner_enabled or not settings.llm_api_key:
                raise SystemExit("Real planning must be configured")
            manifest["export"] = await run_cases(session, settings, folder, suite)
            manifest["code_after"] = code_state()
            write_json(data_dir / "environment.json", manifest)
    finally:
        await engine.dispose()


async def run_cases(session, settings, folder, suite):
    from app.core import loop_tracing as trace
    from app.core.loop_budget import LoopBudget
    from app.core.loop_models import LoopUsage, TextModel
    from app.core.strands_model import openai_client
    from app.tools.catalog_search.planner import CatalogSearchPlanner
    from app.tools.contracts import build_catalog_search_tool, execute_bound_tool

    # Reuse existing request-local trace context without invoking chat runtime.
    tokens = [(trace._secrets, trace._secrets.set(tuple(v for v in (
        settings.llm_api_key, settings.auth_token_secret,
        settings.tavily_api_key.get_secret_value(),
        settings.langsmith_api_key.get_secret_value()) if v)))]
    try:
        for case in suite["cases"]:
            budget = LoopBudget()
            usage = LoopUsage(budget)
            last = trace._last_model.set({})
            scope = trace._scope.set(None)
            record = {"case_id": case["id"], "batch_id": folder.name, "attempt": 1,
                      "started_at": now(), "arguments": case["input"], "trace_id": None}
            try:
                with trace.step("商品工具测试 · " + case["id"], root=True) as span:
                    if span:
                        record["trace_id"] = format(span.context.trace_id, "032x")
                    trace.fields(batch_id=folder.name, case_id=case["id"], attempt=1,
                                 runtime="catalog_tool_pilot", model=settings.llm_model)
                    trace.payload("input", case["input"])
                    async with openai_client(settings, usage,
                                             client_class=trace.ModelHTTPClient) as client:
                        planner = CatalogSearchPlanner(TextModel(client, settings, budget))
                        bound = build_catalog_search_tool(session, settings=settings, planner=planner)
                        budget.before_tool(None)
                        with budget.tool_scope(None), trace.step("工具 · catalog_search", "TOOL"):
                            trace.fields(batch_id=folder.name, case_id=case["id"], attempt=1,
                                         tool_name="catalog_search")
                            trace.payload("input", case["input"])
                            result = await execute_bound_tool(bound, case["input"], {"user_id": 0})
                            record["result"] = result.model_dump(mode="json")
                            trace.payload("output", record["result"])
                    trace.payload("output", record.get("result"))
            except Exception as exc:  # noqa: BLE001 - persist every failed live attempt
                record["harness_error"] = {"type": type(exc).__name__,
                                           "message": trace.clean(str(exc))}
            finally:
                record.update(finished_at=now(), usage=usage.summary(),
                              budget_calls=dict(budget.calls), tool_attempts=budget.tool_attempts)
                with (folder / "evidence/executions.jsonl").open("a") as handle:
                    handle.write(json.dumps(trace.clean(record), ensure_ascii=False) + "\n")
                trace._scope.reset(scope)
                trace._last_model.reset(last)
            print(json.dumps({"case": case["id"], "trace_id": record["trace_id"],
                              "http_attempts": usage.attempts}, ensure_ascii=False), flush=True)
        return {
            "flush_completed": trace.provider().force_flush(timeout_millis=2000),
            "notice": "Flush is not receiver verification; check_price_lookup.py fetches Phoenix"}
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
        trace.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--run", metavar="BATCH")
    asyncio.run(main(parser.parse_args()))
