from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.tools.contracts import DefaultToolContractProvider, RegistryToolExecutor

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = ROOT / "evals" / "catalog_llm_eval_cases.json"

WIRELESS_VALUES = {
    "wireless",
    "bluetooth",
    "wifi",
    "蓝牙",
    "无线",
    "三模",
    "2.4g",
    "2.4g无线",
    "2.4g 无线",
    "是",
    "true",
    "yes",
}
WIRED_VALUES = {
    "wired",
    "usb",
    "usb-a",
    "usb-c",
    "cable",
    "有线",
    "否",
    "false",
    "no",
}
COLOR_ALIASES = {
    "black": {"black", "黑", "黑色"},
    "white": {"white", "白", "白色"},
    "silver": {"silver", "银", "银色"},
    "gray": {"gray", "grey", "灰", "灰色"},
    "pink": {"pink", "粉", "粉色"},
}


@dataclass
class EvalResult:
    case_id: str
    query: str
    tool: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def load_cases(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def norm(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")


def value_matches(actual: Any, expected: Any) -> bool:
    actual_norm = norm(actual)
    expected_norm = norm(expected)
    if actual_norm == expected_norm:
        return True
    if expected_norm == "wireless":
        return actual_norm in {item.replace(" ", "") for item in WIRELESS_VALUES}
    if expected_norm == "wired":
        return actual_norm in {item.replace(" ", "") for item in WIRED_VALUES}
    if expected_norm == "yes":
        return actual_norm in {"yes", "true", "1", "是", "有", "带"}
    if expected_norm == "no":
        return actual_norm in {"no", "false", "0", "否", "无", "不带"}
    if expected_norm in COLOR_ALIASES:
        return actual_norm in {item.replace(" ", "") for item in COLOR_ALIASES[expected_norm]}
    return expected_norm in actual_norm


def product_matches_specs(product: dict[str, Any], expected_specs: dict[str, Any]) -> bool:
    specs = product.get("specs") or {}
    for key, expected in expected_specs.items():
        if key.endswith("_contains_any"):
            real_key = key.removesuffix("_contains_any")
            actual = str(specs.get(real_key, ""))
            if not any(str(item).lower() in actual.lower() for item in expected):
                return False
            continue
        if key not in specs:
            return False
        if not value_matches(specs[key], expected):
            return False
    return True


def decimal_equal(actual: Any, expected: Any) -> bool:
    if actual is None:
        return False
    return Decimal(str(actual)) == Decimal(str(expected))


def assert_output(case: dict[str, Any], output: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    expect = case["expect"]
    query_plan = output.get("query_plan") or {}
    diagnostics = output.get("diagnostics") or []

    if output.get("result_type") != expect.get("result_type"):
        failures.append(
            f"result_type expected {expect.get('result_type')}, got {output.get('result_type')}"
        )

    if expected_error := expect.get("error_type"):
        if query_plan.get("error_type") != expected_error:
            failures.append(
                f"error_type expected {expected_error}, got {query_plan.get('error_type')}"
            )
    if expected_code := expect.get("diagnostic_code"):
        actual_code = diagnostics[0].get("code") if diagnostics else None
        if actual_code != expected_code:
            failures.append(f"diagnostic_code expected {expected_code}, got {actual_code}")

    if expected_category := expect.get("category"):
        actual_category = query_plan.get("category")
        if actual_category != expected_category:
            failures.append(f"category expected {expected_category}, got {actual_category}")

    if expected_filters := expect.get("filters"):
        actual_filters = query_plan.get("filters") or {}
        for key, expected_value in expected_filters.items():
            if key not in actual_filters:
                failures.append(f"missing query_plan filter {key}")
            elif not value_matches(actual_filters[key], expected_value):
                failures.append(
                    f"filter {key} expected {expected_value}, got {actual_filters[key]}"
                )

    if expected_brands := expect.get("brands"):
        actual_brands = query_plan.get("brands") or []
        missing = [brand for brand in expected_brands if brand not in actual_brands]
        if missing:
            failures.append(f"missing query_plan brands {missing}, got {actual_brands}")

    if expected_min_price := expect.get("min_price"):
        if not decimal_equal(query_plan.get("min_price"), expected_min_price):
            failures.append(
                f"min_price expected {expected_min_price}, got {query_plan.get('min_price')}"
            )
    if expected_max_price := expect.get("max_price"):
        if not decimal_equal(query_plan.get("max_price"), expected_max_price):
            failures.append(
                f"max_price expected {expected_max_price}, got {query_plan.get('max_price')}"
            )
    if expected_purpose := expect.get("result_purpose"):
        if output.get("result_purpose") != expected_purpose:
            failures.append(
                f"result_purpose expected {expected_purpose}, "
                f"got {output.get('result_purpose')}"
            )
    if expected_scope := expect.get("selection_scope"):
        if output.get("selection_scope") != expected_scope:
            failures.append(
                f"selection_scope expected {expected_scope}, "
                f"got {output.get('selection_scope')}"
            )
    for field_name in expect.get("requested_fields", []):
        if field_name not in query_plan.get("requested_fields", []):
            failures.append(f"requested field {field_name} was not preserved")

    if case["tool"] == "catalog_facets":
        items = output.get("items") or []
        if len(items) < expect.get("min_item_count", 0):
            failures.append(
                f"facet item count expected >= {expect['min_item_count']}, got {len(items)}"
            )
        if expected_facet := expect.get("facet"):
            if output.get("facet") != expected_facet:
                failures.append(f"facet expected {expected_facet}, got {output.get('facet')}")
        if expected_spec_key := expect.get("spec_key"):
            if output.get("spec_key") != expected_spec_key:
                failures.append(
                    f"spec_key expected {expected_spec_key}, got {output.get('spec_key')}"
                )
        values = {item.get("value") for item in items}
        for value in expect.get("contains_values", []):
            if value not in values:
                failures.append(f"facet value {value} not found in {sorted(values)}")
        if expected_count_scope := expect.get("count_scope"):
            if items and any(
                item.get("count_scope") != expected_count_scope for item in items
            ):
                failures.append(
                    f"all facet items should use count_scope {expected_count_scope}"
                )
        return failures

    products = output.get("products") or []
    min_product_count = expect.get("min_product_count", 0)
    if len(products) < min_product_count:
        failures.append(f"product count expected >= {min_product_count}, got {len(products)}")

    if expected_brand := expect.get("product_brand"):
        if not products or any(product.get("brand") != expected_brand for product in products):
            failures.append(f"all products should be brand {expected_brand}")
    if expected_title_term := expect.get("product_title_contains"):
        if not products or any(
            norm(expected_title_term) not in norm(product.get("title"))
            for product in products
        ):
            failures.append(
                f"all product titles should contain {expected_title_term}"
            )

    if expected_price_lte := expect.get("product_price_lte"):
        max_price = Decimal(str(expected_price_lte))
        if products and any(Decimal(str(product.get("price"))) > max_price for product in products):
            failures.append(f"all product prices should be <= {expected_price_lte}")

    if expected_any := expect.get("contains_brands_any"):
        brands = {product.get("brand") for product in products}
        if not brands.intersection(expected_any):
            failures.append(f"expected any brand in {expected_any}, got {sorted(brands)}")

    if expected_specs := expect.get("product_specs"):
        if products and not all(
            product_matches_specs(product, expected_specs) for product in products
        ):
            failures.append(f"not all products match expected specs {expected_specs}")

    return failures


def summarize_output(output: dict[str, Any]) -> dict[str, Any]:
    query_plan = output.get("query_plan") or {}
    products = output.get("products") or []
    items = output.get("items") or []
    diagnostics = output.get("diagnostics") or []
    return {
        "result_type": output.get("result_type"),
        "error_type": query_plan.get("error_type"),
        "diagnostic_code": diagnostics[0].get("code") if diagnostics else None,
        "planner": query_plan.get("planner"),
        "category": query_plan.get("category"),
        "brands": query_plan.get("brands"),
        "filters": query_plan.get("filters"),
        "product_count": len(products),
        "facet_count": len(items),
        "top_titles": [product.get("title") for product in products[:2]],
    }


async def run_case(
    executor: RegistryToolExecutor,
    provider: DefaultToolContractProvider,
    case: dict[str, Any],
) -> EvalResult:
    tool = case["tool"]
    contract = provider.get_contract(tool)
    if contract is None:
        return EvalResult(case["id"], case["query"], tool, False, [f"unknown tool {tool}"])
    args = {"query": case["query"], "limit": case.get("limit", 3)}
    runtime_context = {"user_id": 1, **case.get("runtime_context", {})}
    result = await executor.execute(contract, args, runtime_context)
    if not result.ok:
        error = result.error.model_dump(mode="json") if result.error else {}
        return EvalResult(case["id"], case["query"], tool, False, [f"tool error {error}"])
    output = result.output or {}
    failures = assert_output(case, output)
    return EvalResult(
        case_id=case["id"],
        query=case["query"],
        tool=tool,
        passed=not failures,
        failures=failures,
        summary=summarize_output(output),
    )


async def run_eval(cases: list[dict[str, Any]], case_ids: list[str] | None) -> list[EvalResult]:
    selected_ids = set(case_ids or [])
    selected = [case for case in cases if not selected_ids or case["id"] in selected_ids]
    settings = get_settings()
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required for real catalog LLM eval")
    if not settings.catalog_llm_planner_enabled:
        raise RuntimeError("CATALOG_LLM_PLANNER_ENABLED must be true for real catalog LLM eval")

    async with AsyncSessionLocal() as session:
        revision = (
            await session.execute(text("select version_num from alembic_version"))
        ).scalar_one_or_none()
        print(json.dumps({"db_migration": revision}, ensure_ascii=False))
        provider = DefaultToolContractProvider()
        executor = RegistryToolExecutor(session, settings=settings)
        results = []
        for case in selected:
            result = await run_case(executor, provider, case)
            results.append(result)
            print(
                json.dumps(
                    {
                        "id": result.case_id,
                        "passed": result.passed,
                        "tool": result.tool,
                        "query": result.query,
                        "summary": result.summary,
                        "failures": result.failures,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real LLM + PostgreSQL catalog tool evals.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--case-id", action="append", default=None)
    args = parser.parse_args()

    cases = load_cases(args.cases)
    results = asyncio.run(run_eval(cases, args.case_id))
    failed = [result for result in results if not result.passed]
    print(
        json.dumps(
            {"total": len(results), "passed": len(results) - len(failed), "failed": len(failed)},
            ensure_ascii=False,
        )
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
