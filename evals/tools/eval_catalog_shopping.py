
"""Live model + synthetic catalog/order planning; no database or chat persistence.

Run explicitly: .venv/bin/python -m evals.tools.eval_catalog_shopping
Use --compare for baseline/review, --review-probes for seeded semantic mistakes.
Only model API calls incur cost. Expectations are hand-written results and semantics.
"""

import argparse
import asyncio
import json
import time
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.llm import build_chat_model
from app.core.llm_calls import ModelCallError, transport_failure_details
from app.core.model_usage import collect_model_calls, model_call_counts
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.orders import LLMOrderQueryPlanner
from app.tools.schemas import OrderLookupInput

from evals.support.paths import RESULTS, ROOT, project_path
from tests.support.tool_plan_fixtures import facet_plan, order_plan, search_plan
from tests.support.tool_planning import Repository, product

# Independent natural paraphrases, not copied from the scripted regression inputs.
CASES = [
    ("给我找鼠标，蓝牙和2.4G两种连接都得有", {1}, {"蓝牙", "2.4g"}),
    ("想买三模鼠标，蓝牙也要支持", {1}, {"三模"}),
    ("鼠标连接用蓝牙或2.4G都行", {1, 2, 3}, set()),
    ("无线鼠标里面，不支持蓝牙的有吗", {3}, set()),
    ("白色的蓝牙鼠标，预算不超过200元", {1}, {"蓝牙"}),
    ("我需要鼠标既能有线连接，又能蓝牙连接", {1}, {"有线", "蓝牙"}),
]


def check_connection_requirements(plan, required):
    """Independent oracle: retain requested conjunctions, not a coincidentally matching SKU."""
    aliases = {"2.4g无线": "2.4g", "bluetooth": "蓝牙", "wired": "有线"}
    actual = set()
    for condition in plan.get("conditions", []):
        if (condition.get("key") != "connection_type"
                or condition.get("operator") != "eq"
                or condition.get("strength") != "requirement"):
            continue
        for value in condition.get("values", []):
            normalized = "".join(value.lower().split())
            actual.add(aliases.get(normalized, normalized))
    return required <= actual and ("三模" not in actual or "三模" in required)


def catalog():
    rows = [
        product(1, 1, category="鼠标", price="180", connection_type="三模", color="白色"),
        product(2, 2, category="鼠标", price="120", connection_type="蓝牙", color="黑色"),
        product(3, 3, category="鼠标", price="90", connection_type="2.4G", color="白色"),
        product(4, 4, category="鼠标", price="60", connection_type="有线", color="白色"),
    ]
    for row in rows:
        row.product.brand = f"测试品牌{row.product.sku_id}"
        row.product.specs["wireless"] = "否" if row.product.sku_id == 4 else "是"
    return Repository(rows)


class RecordedModel:
    def __init__(self, model, initial=None):
        self.model = model
        self.initial = initial
        self.outputs = []
        self.api_calls = 0
        self.attempts = []

    async def ainvoke(self, messages):
        if self.initial is not None and not self.outputs:
            result = SimpleNamespace(content=json.dumps(self.initial))
        else:
            self.api_calls += 1
            started = time.perf_counter()
            record = {"stage": "tool_review" if "candidate_plan" in json.loads(messages[1].content)
                      else "tool_planning"}
            try:
                result = await self.model.ainvoke(messages)
                record["outcome"] = "success"
            except BaseException as exc:
                record.update(outcome="error", **transport_failure_details(exc))
                raise
            finally:
                record["seconds"] = round(time.perf_counter() - started, 3)
                self.attempts.append(record)
        try:
            self.outputs.append(json.loads(result.content))
        except (ValueError, TypeError):
            self.outputs.append({"invalid_json": True})
        return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--compare", action="store_true", help="Run baseline and reviewed planning")
    modes.add_argument("--acceptance", action="store_true",
                       help="Reviewed regression plus new paraphrases absent from prompts")
    modes.add_argument("--review-probes", action="store_true",
                       help="Seed four known semantic mistakes; live-review and repair them")
    modes.add_argument("--retry-timeouts", type=project_path,
                       help="Recheck only model timeouts in an earlier report; preserve it")
    args = parser.parse_args()
    retry_cases = None
    if args.retry_timeouts:
        previous = json.loads(args.retry_timeouts.read_text())
        retry_cases = {(c["tool"], c["query"], c["review_enabled"])
                       for c in previous["cases"] if c.get("error_code") == "planning_call_timeout"}
    settings = get_settings()
    model = build_chat_model(settings, temperature=0, max_retries=0)
    if model is None:
        raise SystemExit("Model not configured; no evaluation performed")
    report = {"mode": "live_model_synthetic_catalog", "model": settings.llm_model, "cases": []}
    report["implementation_sha256"] = {
        str(path): sha256(path.read_bytes()).hexdigest()
        for path in [(ROOT / "backend/app/tools/prompts/catalog_search.py"),
                     (ROOT / "backend/app/tools/prompts/catalog_search_specs.py"),
                     (ROOT / "backend/app/tools/prompts/plan_review.py"),
                     (ROOT / "backend/app/tools/catalog_fields.py"),
                     (ROOT / "backend/app/tools/structured_planning.py"),
                     (ROOT / "backend/app/core/llm_calls.py"),
                     Path(__file__).resolve()]
    }
    if args.retry_timeouts:
        report["retry_source"] = str(args.retry_timeouts)

    async def run(tool, query, expected=None, reasons=None, *, review=True, initial=None,
                  all_connections=None, preferred_connection=None, cohort="regression"):
        if retry_cases is not None and (tool, query, review) not in retry_cases:
            return
        recorded = RecordedModel(model, initial)
        started = time.perf_counter()
        repository = catalog()
        handler = (
            CatalogSearchService(repository, CatalogSearchPlanner(recorded, review=review)).search
            if tool == "catalog_search"
            else CatalogToolService(
                None, planner=LLMCatalogQueryPlanner(recorded, review=review),
            ).facets
        )
        with collect_model_calls(), patch(
            "app.repositories.catalog_search.CatalogSearchRepository.load",
            lambda self, category, scope: repository.load(category, scope),
        ):
            result = await execute_bound_tool(
                BoundTool(DefaultToolContractProvider().get_contract(tool), handler),
                {"query": query, "limit": 20}, {"user_id": 7},
            )
            counts = model_call_counts()
        output = result.output or {}
        passed = result.ok
        if expected is not None:
            actual = (
                {sku for p in output.get("products", []) for sku in p["candidate_sku_ids"]}
                if tool == "catalog_search"
                else {i["value"] for i in output.get("items", [])}
            )
            wanted = expected if tool == "catalog_search" else {f"测试品牌{i}" for i in expected}
            passed = passed and actual == wanted and bool(repository.calls)
        else:
            diagnostics = output.get("diagnostics", [])
            actual = {d["code"] for d in diagnostics}
            for d in diagnostics:
                actual.update(i["reason_code"] for i in d.get("details", {}).get("issues", []))
            passed = (
                passed and reasons <= actual and not repository.calls
                and output.get("matching_complete") is False
                and output.get("is_exhaustive") is False
            )
        semantic_ok = (not all_connections or check_connection_requirements(
            output.get("query_plan", {}), all_connections,
        ))
        if preferred_connection:
            semantic_ok = semantic_ok and any(
                c.get("key") == "connection_type" and c.get("strength") == "preference"
                and any(preferred_connection in v.lower() for v in c.get("values", []))
                for c in output.get("query_plan", {}).get("conditions", [])
            )
        passed = passed and semantic_ok
        report["cases"].append({
            "tool": tool, "query": query, "passed": passed, "review_enabled": review,
            "model_calls": counts, "seconds": round(time.perf_counter() - started, 3),
            "model_api_calls": recorded.api_calls,
            "model_outputs": recorded.outputs, "seeded_initial_plan": initial is not None,
            "expected": sorted(expected if expected is not None else reasons),
            "semantic_passed": semantic_ok,
            "required_connections": sorted(all_connections or []),
            "preferred_connection": preferred_connection,
            "repository_reads": len(repository.calls), "output": output,
            "error_code": result.error.code if result.error else None,
            "error_details": result.error.details if result.error else None,
            "model_attempts": recorded.attempts, "cohort": cohort,
        })
        print(f"{'review' if review else 'baseline'} {tool}: "
              f"{'PASS' if passed else 'FAIL'} {query}", flush=True)

    async def run_order(query, expected, *, review, initial=None, cohort="regression"):
        if (retry_cases is not None
                and ("order_planning_only", query, review) not in retry_cases):
            return
        recorded = RecordedModel(model, initial)
        started = time.perf_counter()
        error = None
        error_details = None
        plan = None
        with collect_model_calls():
            try:
                plan = await LLMOrderQueryPlanner(recorded, review=review).plan(
                    OrderLookupInput(user_id=7, query=query),
                )
            except ModelCallError as exc:
                error = exc.code
                error_details = exc.details
            counts = model_call_counts()
        output = plan.model_dump(mode="json") if plan else {}
        actual = output.get("filters", {})
        passed = error is None
        for key, value in expected.items():
            if key == "requested_fields":
                passed = passed and set(value) <= set(output.get(key, []))
            elif key in {"operation", "selection"}:
                passed = passed and output.get(key) == value
            elif key in {"start_at", "end_at"}:
                passed = passed and str(actual.get(key, "")).startswith(value)
            else:
                passed = passed and actual.get(key) == value
        report["cases"].append({
            "tool": "order_planning_only", "query": query, "passed": passed,
            "review_enabled": review, "model_calls": counts,
            "model_api_calls": recorded.api_calls,
            "seconds": round(time.perf_counter() - started, 3),
            "expected": expected, "output": output, "error_code": error,
            "model_outputs": recorded.outputs, "seeded_initial_plan": initial is not None,
            "repository_reads": 0,
            "error_details": error_details, "model_attempts": recorded.attempts, "cohort": cohort,
        })
        print(f"{'review' if review else 'baseline'} order: "
              f"{'PASS' if passed else 'FAIL'} {query}", flush=True)

    for review in ([] if args.review_probes or args.retry_timeouts else [False, True]
                   if args.compare else [True]):
        for query, expected, connections in CASES:
            for tool in ("catalog_search", "catalog_facets"):
                utterance = query + ("，有哪些品牌" if tool == "catalog_facets" else "")
                await run(tool, utterance, expected, review=review, all_connections=connections)
        for query, reasons in [
            ("能无线充电的鼠标都有哪些品牌", {"filter_data_unavailable"}),
            ("办公用、还能无线充电的鼠标有哪些品牌", {
                "filter_data_unavailable", "requirement_needs_clarification",
            }),
            ("这款商品都有什么规格", {"query_needs_clarification"}),
            ("Logitech 影刃 M01的规格有哪些", {"product_identity_required"}),
        ]:
            await run("catalog_facets", query, reasons=reasons, review=review)
        if args.compare or args.retry_timeouts or args.acceptance:
            for query, expected in [
                ("我买的键盘里，待发货订单有几笔", {
                    "operation": "total_count", "statuses": [2], "product_terms": ["键盘"],
                }),
                ("查2026年9月付款的订单有多少笔", {
                    "operation": "total_count", "time_field": "pay_at",
                    "start_at": "2026-09-01T00:00:00", "end_at": "2026-10-01T00:00:00",
                }),
                ("最近一笔订单现在运到哪了", {
                    "selection": "latest", "requested_fields": ["latest_logistics_event"],
                }),
                ("我最近那笔订单现在到哪了，大概还要几天能收到", {
                    "selection": "latest",
                    "requested_fields": ["latest_logistics_event", "estimated_delivery_time"],
                }),
            ]:
                await run_order(query, expected, review=review)
    if args.retry_timeouts:
        for case in previous["cases"]:
            if case.get("error_code") != "planning_call_timeout":
                continue
            if case["tool"] == "order_planning_only":
                await run_order(case["query"], case["expected"], review=case["review_enabled"],
                                cohort=case.get("cohort", "regression"))
            else:
                expected = set(case["expected"])
                is_catalog_result = all(isinstance(value, int) for value in expected)
                await run(case["tool"], case["query"],
                          expected if is_catalog_result else None,
                          None if is_catalog_result else expected,
                          review=case["review_enabled"],
                          all_connections=set(case.get("required_connections", [])),
                          preferred_connection=case.get("preferred_connection"),
                          cohort=case.get("cohort", "regression"))
    if args.acceptance:
        for query, expected, connections, preference in [
            ("这只鼠标既得连蓝牙，也得能用2.4G，少一种都不行", {1}, {"蓝牙", "2.4g"}, None),
            ("给我看看鼠标，蓝牙和2.4G有一种能用就够", {1, 2, 3}, set(), None),
            ("鼠标能连蓝牙就满足要求了，如果还有2.4G会更好", {1, 2}, {"蓝牙"}, "2.4g"),
            ("买鼠标，用2.4G连接，但不要带蓝牙的", {3}, {"2.4g"}, None),
            ("鼠标要蓝牙和2.4G一起支持，价格控制在190元以内", {1}, {"蓝牙", "2.4g"}, None),
            ("我想买三模鼠标，必须能连蓝牙", {1}, {"三模"}, None),
        ]:
            for tool in ("catalog_search", "catalog_facets"):
                utterance = query + ("，有哪些品牌" if tool == "catalog_facets" else "")
                await run(tool, utterance, expected, all_connections=connections,
                          preferred_connection=preference, cohort="new_paraphrase")
        await run("catalog_facets", "能用充电板补电的鼠标，有哪些品牌可以选",
                  reasons={"filter_data_unavailable"}, cohort="new_paraphrase")
        await run("catalog_facets", "前面提到的这款，想看看有哪些规格",
                  reasons={"query_needs_clarification"}, cohort="new_paraphrase")
        await run_order("帮我数数买过的鼠标订单里，还有多少笔没发货", {
            "operation": "total_count", "statuses": [2], "product_terms": ["鼠标"],
        }, review=True, cohort="new_paraphrase")
        await run_order("今年九月份付过款的订单一共有几单", {
            "operation": "total_count", "time_field": "pay_at",
            "start_at": f"{datetime.now(ZoneInfo('Asia/Shanghai')).year}-09-01T00:00:00",
            "end_at": f"{datetime.now(ZoneInfo('Asia/Shanghai')).year}-10-01T00:00:00",
        }, review=True, cohort="new_paraphrase")
    if args.review_probes:
        report["mode"] = "seeded_plan_live_review_synthetic_catalog"
        await run("catalog_search", "鼠标的蓝牙和2.4G两种连接必须都有", {1}, initial=search_plan({
            "category": "鼠标", "conditions": [{
                "key": "connection_type", "operator": "in", "values": ["蓝牙", "2.4G"],
                "evidence": "蓝牙和2.4G两种连接必须都有",
            }],
        }), all_connections={"蓝牙", "2.4g"})
        await run("catalog_search", "想找150元以内的蓝牙鼠标", {2}, initial=search_plan({
            "category": "鼠标", "conditions": [{
                "key": "connection_type", "values": ["蓝牙"], "evidence": "蓝牙",
            }],
        }))
        await run("catalog_facets", "支持无线充电的鼠标有哪些品牌", reasons={
            "filter_data_unavailable",
        }, initial=facet_plan({"category": "鼠标", "conditions": [{
            "key": "wireless", "values": ["是"], "evidence": "无线充电",
        }]}))
        initial = order_plan({"operation": "total_count", "requested_fields": ["order_count"],
                              "filter_evidence": ["键盘"]})
        initial["filters"]["product_terms"] = ["键盘"]
        await run_order("我买的键盘里，待发货订单有几笔", {
            "operation": "total_count", "statuses": [2], "product_terms": ["键盘"],
        }, review=True, initial=initial)
    report["passed"] = sum(case["passed"] for case in report["cases"])
    report["total"] = len(report["cases"])
    report["comparison"] = {
        label: {
            "passed": sum(c["passed"] for c in report["cases"] if c["review_enabled"] == enabled),
            "total": sum(c["review_enabled"] == enabled for c in report["cases"]),
            "model_calls": sum(sum(c["model_calls"].values()) for c in report["cases"]
                               if c["review_enabled"] == enabled),
            "seconds": round(sum(c["seconds"] for c in report["cases"]
                                 if c["review_enabled"] == enabled), 3),
        }
        for label, enabled in [("baseline", False), ("review", True)]
    }
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    path = RESULTS / f"catalog-shopping-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"{report['passed']}/{report['total']} passed; report: {path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
