"""Read-only formal-tool evaluation. Never calls chat, seed, migrations or other tools."""

import argparse
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.core.config import get_settings
from app.schemas.catalog_search import SearchCondition
from app.tools.catalog.planner import explicit_lookup
from app.tools.catalog.fields import matches, normalize, number
from app.tools.contracts import (
    CatalogSearchPublicInput,
    build_catalog_search_tool,
    execute_bound_tool,
)
from app.tools.prompts.catalog_search import PROMPT_VERSION
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from evals.support.paths import project_path

ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = ROOT / "evals/tools/catalog_search"
SUITES = ("classic", "boundary", "fallback")
DEFAULT_REPORT = ROOT / "artifacts/evals/catalog_search/catalog_search.json"


def load_cases(suite, *, root=EVAL_ROOT):
    """Only current legal tool inputs; retired suites are never silently selected."""
    if suite not in (*SUITES, "all"):
        raise ValueError("unknown_suite")
    selected = SUITES if suite == "all" else (suite,)
    cases, identities = [], set()
    for name in selected:
        path = root / f"inputs/catalog_search_{name}_cases.json"
        for case in json.loads(path.read_text()):
            if case["id"] in identities:
                raise ValueError("duplicate_case_id")
            identities.add(case["id"])
            CatalogSearchPublicInput.model_validate(
                {"query": case["query"], **case.get("args", {})}
            )
            cases.append({**case, "suite": name})
    if not cases:
        raise ValueError("no_cases_selected")
    return cases


def verify(case, result, oracle, candidate_oracle=None):
    expected = case["expect"]
    errors = []
    if "error_code" in expected:
        return (
            []
            if (
                not result.ok
                and result.error.code == expected["error_code"]
                and result.output is None
            )
            else ["expected typed execution error"]
        )
    if not result.ok:
        return [f"tool error: {result.error.code}"]
    output = result.output
    if "scope" in expected and output.get("selection_scope") != expected["scope"]:
        errors.append(f"selection scope: expected {expected['scope']}")
    for key in (
        "status",
        "reason_code",
        "data_quality",
        "matching_complete",
        "is_exhaustive",
        "query_state",
        "result_type",
        "returned_count",
        "unknown_sku_count",
        "ranking_strategy",
        "match_kind",
        "fallback_attempted",
        "total_match_count",
        "candidate_count",
    ):
        if key in expected and output.get(key) != expected[key]:
            errors.append(f"{key}: expected {expected[key]}, actual {output.get(key)}")
    if "condition_keys" in expected:
        actual_keys = {c["key"] for c in output.get("query_plan", {}).get("conditions", [])}
        if actual_keys != set(expected["condition_keys"]):
            errors.append(
                f"condition keys: expected {sorted(expected['condition_keys'])}, "
                f"actual {sorted(actual_keys)}"
            )
    if "allowed_condition_keys" in expected:
        actual_keys = {c["key"] for c in output.get("query_plan", {}).get("conditions", [])}
        unexpected = actual_keys - set(expected["allowed_condition_keys"])
        if unexpected:
            errors.append(f"unexpected condition keys: {sorted(unexpected)}")
    # Independent expected decisions expose conditions masked by correlated SKU
    # data (e.g. every matching layout already happens to have no backlight).
    for probe in expected.get("condition_probes", []):
        conditions = [
            SearchCondition.model_validate(c)
            for c in output.get("query_plan", {}).get("conditions", [])
            if c["key"] == probe["key"]
        ]
        decisions = [matches(probe["actual"], c) for c in conditions]
        actual = False if False in decisions else None if None in decisions else True
        if not conditions or actual is not probe["matches"]:
            errors.append(
                f"condition probe {probe['key']}={probe['actual']}: "
                f"expected {probe['matches']}, actual {actual if conditions else 'absent'}"
            )
    products = output.get("products", [])
    for key in ("sku_ids", "spu_ids"):
        if key in expected:
            actual = [p[key[:-1]] for p in products]
            if actual != expected[key]:
                errors.append(f"{key}: expected {expected[key]}, actual {actual}")
    if "object_statuses" in expected:
        actual = [obj["status"] for obj in output["object_results"]]
        if actual != expected["object_statuses"]:
            errors.append(f"object statuses: {actual}")
    issues = [issue for p in products for issue in p.get("field_issues", [])]
    for key, status in (("missing_field", "missing"), ("conflict_field", "conflict")):
        if key in expected and not any(
            item["field"] == expected[key] and item["status"] == status for item in issues
        ):
            errors.append(f"missing {status} evidence for {expected[key]}")
    if oracle is not None:
        allowed = {row["sku_id"] for row in oracle}
        models = {row["spu_id"] for row in oracle}
        if output["matched_sku_count"] != len(allowed):
            errors.append(
                f"SKU count: expected {len(allowed)}, actual {output['matched_sku_count']}"
            )
        if output["matched_spu_count"] != len(models):
            errors.append(
                f"SPU count: expected {len(models)}, actual {output['matched_spu_count']}"
            )
        product_oracle = candidate_oracle if candidate_oracle is not None else oracle
        allowed = {row["sku_id"] for row in product_oracle}
        if candidate_oracle is not None:
            if output["candidate_sku_count"] != len(allowed):
                errors.append("wrong alternative/object candidate SKU count")
            if output["candidate_spu_count"] != len({row["spu_id"] for row in product_oracle}):
                errors.append("wrong alternative/object candidate SPU count")
        for p in products:
            actual_ids = set(p["candidate_sku_ids"])
            if not actual_ids <= allowed:
                errors.append(f"unmatched SKUs leaked into result: {sorted(actual_ids - allowed)}")
            source = next((row for row in product_oracle if row["sku_id"] == p["sku_id"]), None)
            if source is not None:
                for key in ("spu_title", "brand", "category", "sales_count", "description"):
                    expected_value = source[key] or None if key == "description" else source[key]
                    if p.get(key) != expected_value:
                        errors.append(f"incomplete product fact: {key} for SKU {p['sku_id']}")
            variants = p["series_variants"] if p["entity_scope"] == "spu" else [p]
            for variant in variants:
                source = next(
                    (row for row in product_oracle if row["sku_id"] == variant["sku_id"]), None
                )
                if source is None:
                    errors.append("variant not in independent expected set")
                    continue
                for key in ("title", "stock", "sku_sales_count", "image_url"):
                    if variant.get(key) != source[key]:
                        errors.append(f"incomplete version fact: {key} for SKU {source['sku_id']}")
                if Decimal(variant["price"]) != source["price"]:
                    errors.append(f"wrong version price for SKU {source['sku_id']}")
                if variant.get("specs") != source["specs_json"]:
                    errors.append(f"incomplete recorded specs for SKU {source['sku_id']}")
            if p["entity_scope"] == "spu":
                matching_rows = [row for row in product_oracle if row["spu_id"] == p["spu_id"]]
                if actual_ids != {row["sku_id"] for row in matching_rows}:
                    errors.append("model lost matching variants")
                if matching_rows:
                    low = min(row["price"] for row in matching_rows)
                    high = max(row["price"] for row in matching_rows)
                    if (Decimal(p["series_min_price"]), Decimal(p["series_max_price"])) != (
                        low,
                        high,
                    ):
                        errors.append("model price range does not describe matched variants")
        if sort := expected.get("sort"):
            sku_scope = output.get("selection_scope") == "sku"
            identity_key = "sku_id" if sku_scope else "spu_id"
            groups = {}
            for row in product_oracle:
                groups.setdefault(row[identity_key], []).append(row)

            def metric(rows):
                if sort == "sales":
                    return -rows[0]["sku_sales_count" if sku_scope else "sales_count"]
                price = min(row["price"] for row in rows)
                return price if sort == "price_asc" else -price

            ordered = sorted(
                groups,
                key=lambda k: (metric(groups[k]), k),
            )
            args = case.get("args", {})
            start, limit = args.get("offset", 0), args.get("limit", 3)
            if [p[identity_key] for p in products] != ordered[start : start + limit]:
                errors.append(f"incorrect {sort} {'sku' if sku_scope else 'model'} window")
    for key, operator, value in expected.get("conditions", []):

        def same_value(actual, key=key, value=value):
            lhs, rhs = number(key, actual), number(key, value)
            return (
                lhs == rhs
                if lhs is not None and rhs is not None
                else (normalize(key, actual) == normalize(key, value))
            )

        # Single-value exclusion can be represented by either ne or not_in.
        operators = {operator} | ({"ne", "not_in"} if operator in {"ne", "not_in"} else set())
        if not any(
            c["key"] == key
            and c["operator"] in operators
            and any(same_value(v) for v in c["values"])
            for c in output.get("query_plan", {}).get("conditions", [])
        ):
            errors.append(f"condition lost: {key} {operator} {value}")
    for key, strength in expected.get("condition_strengths", {}).items():
        conditions = [
            c for c in output.get("query_plan", {}).get("conditions", []) if c["key"] == key
        ]
        if not conditions or any(c.get("strength") != strength for c in conditions):
            errors.append(f"condition strength lost: {key}={strength}")
    if expected.get("match_kind") == "alternative":
        for product in products:
            for sku in product["candidate_sku_ids"]:
                checks = [c for c in product["condition_checks"] if c["sku_id"] == sku]
                if len({c["condition"]["key"] for c in checks if c["verdict"] == "mismatch"}) != 1:
                    errors.append("alternative does not have exactly one mismatching dimension")
                if any(c["verdict"] == "unknown" for c in checks):
                    errors.append("unknown fact advertised as an alternative")
    return errors


def markdown_report(report):
    lines = [
        "# 商品搜索工具效果报告",
        "",
        f"执行时间：{report['created_at']}；模式：{report['mode']}；数据库事务：只读。",
        f"用例集：{report.get('suite', 'legacy')}；classic为六类商品经典查询，"
        "boundary为合法请求的业务与hint边界。直接调用正式工具，不运行主编排流。",
        f"解析提示版本：{report.get('prompt_version', 'legacy')}。",
        "",
        f"通过 {report['passed']}/{len(report['results'])}。未调用聊天接口或修改商品数据。",
        "",
        "每条结果包含真实工具输出；通过表示满足该用例断言，不代表任意表达已全面验证。",
        "",
    ]
    if source := report.get("reassessed_from"):
        lines.extend([f"复核来源：{source}。复用首轮模型输出重新核验，未重新调用模型。", ""])
    for item in report["results"]:
        output = item["result"].get("output") or {}
        lines.extend(
            [
                f"## {item['id']}：{'通过' if item['passed'] else '失败'}",
                "",
                f"输入：{item['query']}",
                "",
                f"参数：`{json.dumps(item['arguments'], ensure_ascii=False)}`",
                "",
                f"状态：{output.get('status', 'tool_error')}；耗时：{item['seconds']:.2f}s；"
                f"匹配 {output.get('matched_spu_count')} 个型号 / "
                f"{output.get('matched_sku_count')} 个版本（None 表示未确认）；"
                f"返回 {output.get('returned_count', 0)} 项。",
                "",
            ]
        )
        lines.append(
            f"原因码：{output.get('reason_code')}；资料质量：{output.get('data_quality')}；"
            f"匹配完整：{output.get('matching_complete')}；下一步：{output.get('recommended_action')}。"
        )
        if item["result"].get("error"):
            lines.append(f"执行错误：{item['result']['error']['code']}。")
        for candidate in output.get("candidates", []):
            lines.append(
                f"- 消歧候选 SPU {candidate['spu_id']}：{candidate['title']}（未判定条件匹配）。"
            )
        for p in output.get("products", []):
            price = (
                f"{p['series_min_price']}–{p['series_max_price']}"
                if p["entity_scope"] == "spu"
                else p["price"]
            )
            lines.append(
                f"- {p['title']}：¥{price}；SPU {p['spu_id']}；候选 SKU {p['candidate_sku_ids']}。"
            )
            lines.append(
                f"  - 品牌：{p['brand']}；品类：{p['category']}；"
                f"商品描述：{p.get('description') or '未记录'}。"
            )
            variants = p["series_variants"] if p["entity_scope"] == "spu" else [p]
            for variant in variants:
                lines.append(
                    f"  - SKU {variant['sku_id']}：¥{variant['price']}；"
                    f"规格：{json.dumps(variant['specs'], ensure_ascii=False)}。"
                )
            for issue in p.get("field_issues", []):
                lines.append(f"  - SKU {issue['sku_id']} / {issue['field']}：{issue['status']}。")
        if output.get("unknown_sku_count"):
            lines.append(f"无法确认是否匹配的版本：{output['unknown_sku_count']}。")
        for obj in output.get("object_results", []):
            target = obj.get("target")
            identity = f"{target['kind'].upper()} {target['id']}" if target else obj["name"]
            lines.append(f"- {identity}：{obj['status']}。")
        lines.extend(
            [
                "",
                "解析计划：",
                "",
                "```json",
                json.dumps(
                    output.get("query_plan", {}),
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
            ]
        )
        for d in output.get("diagnostics", []):
            lines.append(f"诊断：{d['message']}")
        for error in item["failures"]:
            lines.append(f"失败原因：{error}")
        lines.append("")
    return "\n".join(lines)


async def evaluate(args):
    settings = get_settings()
    if args.mode == "llm" and not settings.llm_api_key:
        raise RuntimeError("llm_not_configured")
    settings = settings.model_copy(update={"catalog_llm_planner_enabled": args.mode == "llm"})
    suite = args.suite or ("boundary" if args.mode == "ids" else "all")
    cases = load_cases(suite)
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
        if {case["id"] for case in cases} != set(args.case):
            raise ValueError("unknown_case")
    if args.mode == "ids":
        cases = [case for case in cases if explicit_lookup(case["query"]) is not None]
    if not cases:
        raise ValueError("no_cases_selected")
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        connect_args={
            "server_settings": {
                "default_transaction_read_only": "on",
                "statement_timeout": "20000",
            }
        },
        isolation_level="REPEATABLE READ",
    )
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "suite": suite,
        "prompt_version": PROMPT_VERSION,
        "planned_count": len(cases),
        "input_sha256": hashlib.sha256(
            json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "results": [],
    }
    try:
        async with engine.connect() as connection, connection.begin():
            if await connection.scalar(text("SHOW transaction_read_only")) != "on":
                raise RuntimeError("read_only_transaction_required")
            async with AsyncSession(bind=connection, autoflush=False) as session:
                tool = build_catalog_search_tool(session, settings=settings)
                for case in cases:
                    arguments = {"query": case["query"], **case.get("args", {})}
                    start = time.monotonic()
                    result = await execute_bound_tool(tool, arguments, {"user_id": 0})
                    elapsed = time.monotonic() - start
                    oracle_sets = {}
                    for oracle_key in ("oracle", "candidate_oracle"):
                        where = case["expect"].get(oracle_key)
                        if not where:
                            continue
                        # The oracle is a reviewed test fixture, never model-generated SQL.
                        statement = text(
                            "SELECT s.id sku_id, p.id spu_id, s.price, s.title, s.stock, "
                            "s.sales_count sku_sales_count, s.image_url, s.specs_json, "
                            "p.title spu_title, p.detail_html description, p.sales_count, "
                            "b.name brand, c.name category FROM sku s "
                            "JOIN spu p ON s.spu_id=p.id JOIN brand b ON p.brand_id=b.id "
                            "JOIN category c ON p.category_id=c.id "
                            "WHERE s.status=1 AND p.status=1 AND (" + where + ") ORDER BY s.id"
                        )
                        oracle_sets[oracle_key] = (
                            (await session.execute(statement)).mappings().all()
                        )
                    failures = verify(
                        case, result, oracle_sets.get("oracle"), oracle_sets.get("candidate_oracle")
                    )
                    item = {
                        "id": case["id"],
                        "suite": case["suite"],
                        "provenance": case.get("provenance", {}),
                        "query": case["query"],
                        "arguments": arguments,
                        "passed": not failures,
                        "failures": failures,
                        "seconds": elapsed,
                        "result": result.model_dump(mode="json"),
                    }
                    report["results"].append(item)
                    output = result.output or {}
                    print(
                        json.dumps(
                            {
                                "id": case["id"],
                                "passed": not failures,
                                "seconds": round(elapsed, 2),
                                "status": output.get("status", "tool_error"),
                                "counts": [
                                    output.get("matched_spu_count"),
                                    output.get("matched_sku_count"),
                                ],
                                "failures": failures,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    # Save progress after each case so interruption cannot erase evidence.
                    report["passed"] = sum(row["passed"] for row in report["results"])
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    args.output.with_suffix(".md").write_text(markdown_report(report))
    finally:
        await engine.dispose()
    return 0 if all(row["passed"] for row in report["results"]) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("llm", "ids"), default="llm")
    parser.add_argument(
        "--suite",
        choices=(*SUITES, "all"),
        help="Default: all for llm mode; boundary for ids mode",
    )
    parser.add_argument(
        "--case", action="append", help="Run a named case; repeat to select several"
    )
    parser.add_argument("--output", type=project_path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    try:
        code = asyncio.run(evaluate(args))
    except Exception as exc:
        # Provider/connection errors may contain credentials: never print raw exceptions.
        print(json.dumps({"evaluation_error_type": type(exc).__name__}))
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
