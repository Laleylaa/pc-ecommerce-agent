"""Check frozen new-suite evidence. --render is local; --fetch reads local Phoenix.

No database/model/tool calls. Semantic verdicts must be authored by a reviewer.
Original three-case pilot continues using check_price_lookup.py.
"""

import argparse
import copy
import os
import re
import shutil
import urllib.parse
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from check_price_lookup import compact, extract, get_json, status, trace_checks
from run_catalog_cases import GROUPS
from run_price_lookup import (
    batch_path,
    now,
    read_json,
    read_jsonl,
    sha,
    write_json,
    write_jsonl,
)

STATES = {"通过", "失败", "无法判定", "未执行"}
MISSING = {"missing": True}


def value_at(value, path: str):
    parts = path.split(".")
    if not path:
        return value
    first, rest = parts[0], ".".join(parts[1:])
    if first == "*" and isinstance(value, list):
        return [value_at(item, rest) for item in value]
    if isinstance(value, dict) and first in value:
        return value_at(value[first], rest)
    if isinstance(value, list) and first.isdigit() and int(first) < len(value):
        return value_at(value[int(first)], rest)
    return MISSING


def number(value) -> Decimal:
    # Only the predefined yuan-equivalent forms; never import the tested matcher.
    literal = str(value).strip().lower()
    literal = re.sub(r"^(?:人民币|rmb|cny|￥|¥)\s*", "", literal)
    literal = re.sub(r"\s*(?:元|人民币|rmb|cny)$", "", literal)
    result = Decimal(literal)
    if not result.is_finite():
        raise ValueError("Nonfinite number")
    return result


def compare(actual, mode: str, expected) -> bool:
    if actual == MISSING:
        return False
    try:
        if mode in {"eq", "ordered_eq"}:
            return actual == expected
        if mode == "set_eq":
            # Multiplicity must not hide duplicate products or fields.
            return (isinstance(actual, list) and len(actual) == len(expected)
                    and {compact(x) for x in actual} == {compact(x) for x in expected})
        if mode == "required_optional_set":
            required = set(expected["required"])
            allowed = required | set(expected["optional"])
            return (isinstance(actual, list) and len(actual) == len(set(actual))
                    and required <= set(actual) <= allowed)
        if mode == "decimal_eq":
            return number(actual) == number(expected)
        if mode == "decimal_list_eq":
            return isinstance(actual, list) and [number(x) for x in actual] == [
                number(x) for x in expected]
        if mode == "one_of":
            return actual in expected
        if mode == "contains":
            return expected in actual
        if mode == "contains_mapping":
            return isinstance(actual, dict) and all(actual.get(k, MISSING) == v
                                                    for k, v in expected.items())
    except (ValueError, TypeError, ArithmeticError):
        return False
    raise ValueError("Unrecognized comparison: " + mode)


def condition_matches(actual: dict, expected: dict) -> bool:
    if any(actual.get(key) != expected[key] for key in ("key", "operator", "strength")):
        return False
    mode = "decimal_list_eq" if expected.get("numeric") else "eq"
    values = actual.get("values")
    # Enumerated equivalences were written before execution, never inferred from output.
    forms = expected.get("accepted_values", [expected["values"]])
    return any(compare(values, mode, form) for form in forms)


def hard_checks(suite: dict, case: dict, execution: dict) -> list[dict]:
    result = copy.deepcopy(execution.get("result", {}))
    output = result.get("output") or {}
    if output:
        output["diagnostic_codes"] = [row["code"] for row in output.get("diagnostics", [])]
    rows = []
    for field, mode, expected in case.get("common_hard_override", suite["common_hard"]) + case["hard"]:
        actual = value_at(result, field)
        rows.append({"field": field, "comparison": mode, "expected": expected, "actual": actual,
                     "status": "通过" if compare(actual, mode, expected) else "失败"})
    if case["trace_route"] != "input_rejected":
        actual = output.get("query_plan", {}).get("conditions")
        remaining = copy.deepcopy(actual) if isinstance(actual, list) else []
        passed = isinstance(actual, list)
        for expected in case["condition_expectation"]:
            index = next((i for i, row in enumerate(remaining)
                          if condition_matches(row, expected)), None)
            if index is None:
                passed = False
            else:
                remaining.pop(index)
        brand = case.get("allowed_identity_brand")
        # At most one optional identity-brand constraint; no additional restrictions.
        allowed = len(remaining) <= 1 and all(
            brand and c.get("key") == "brand" and c.get("operator") in {"eq", "in"}
            and c.get("strength") == "requirement"
            and [str(v).casefold() for v in c.get("values", [])] == [brand.casefold()]
            for c in remaining)
        rows.append({"field": "output.query_plan.conditions", "comparison": "exact_conditions",
                     "expected": {"required": case["condition_expectation"],
                                  "optional_identity_brand": brand}, "actual": actual,
                     "status": "通过" if passed and allowed else "失败"})
    else:
        attempts = execution.get("usage", {}).get("http_attempts")
        rows.append({"field": "execution.usage.http_attempts", "comparison": "eq",
                     "expected": 0, "actual": attempts,
                     "status": "无法判定" if attempts is None else
                     "通过" if attempts == 0 else "失败"})
    return rows


def trace_rows(case: dict, execution: dict, response: dict) -> tuple[list, dict]:
    raw = response.get("data", [])
    evidence = extract(raw)
    route = case["trace_route"]
    requirements = {"checks": [] if route == "input_rejected" else [
        "计划校验", "复核输出校验", "工具输出校验"], "events": []}
    if route == "model":
        requirements["events"] = ["商品查询上下文", "执行计划确认", "对象定位",
                                   "条件筛选", "排序与返回窗口", "商品查询返回"]
    elif route == "model_stopped":
        requirements["events"] = ["商品查询上下文", "商品查询提前结束"]
    requirements["events"] = ["程序 · " + name for name in requirements["events"]]
    rows = trace_checks({"trace_requirements": requirements}, case, execution, evidence, raw)
    if route == "input_rejected":
        rows = [r for r in rows if r["field"] not in {
            "actual_model_stages", "pre_validation_output"}]
        rows.append({"field": "rejected_before_model", "expected": 0,
                     "actual": len(evidence["models"]),
                     "status": "通过" if not evidence["models"] and raw else "无法判定"})
    usage = execution.get("usage", {})
    calls = execution.get("budget_calls", {})
    stages = usage.get("attempt_stages", [])
    rows.append({"field": "budget_context", "expected": "one tool attempt; model calls scoped",
                 "actual": {"tool_attempts": execution.get("tool_attempts"),
                            "budget_calls": calls, "attempt_stages": stages},
                 "status": "通过" if execution.get("tool_attempts") == 1
                 and usage.get("http_attempts") == len(stages) == sum(calls.values())
                 and all(stage in {"tool_planning", "tool_review"} for stage in stages)
                 else "无法判定"})
    cursor = response.get("next_cursor") or response.get("next_page_token")
    rows.append({"field": "trace_response_pagination", "expected": "no pending page",
                 "actual": cursor, "status": "无法判定" if cursor else "通过"})
    model_ids = {m["span_id"] for m in evidence["models"]}
    linked = [v for v in evidence["validations"] if v["validation"] in {
        "计划校验", "复核输出校验"}]
    rows.append({"field": "validation_model_correlation", "expected": "each model check linked",
                 "actual": [v["model_span_id"] for v in linked],
                 "status": "通过" if (route == "input_rejected" or linked)
                 and all(v["model_span_id"] in model_ids for v in linked) else "无法判定"})
    return rows, evidence


def archive_analysis(folder) -> None:
    files = [p for p in (folder / "report.md", folder / "checks/automatic.jsonl",
                         folder / "checks/semantic.jsonl") if p.exists()]
    if not files:
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    history = folder / "checks/history" / stamp
    history.mkdir(parents=True)
    for path in files:
        shutil.copy2(path, history / path.name)
    write_json(history / "archive.json", {
        "archived_at": now(), "reason": "before explicit re-render; review reason in semantic",
        "review_standard": ["review-v1", "catalog-review-v1"],
        "sha256": {path.name: sha(path) for path in files},
    })


def semantic_rows(suite: dict, case: dict, execution: dict, reviews: list) -> tuple[list, bool]:
    wanted = {r["id"] for r in suite["common_semantic"]}
    matches = [r for r in reviews if r.get("case_id") == case["id"]
               and r.get("batch_id") == execution["batch_id"]
               and r.get("attempt") == execution["attempt"]
               and r.get("trace_id") == execution["trace_id"]]
    if len(matches) != 1:
        return [row for review in matches for row in review.get("checks", [])], False
    review = matches[0]
    rows = review.get("checks", [])
    complete = (
        bool(review.get("reviewer") and review.get("reviewed_at") and review.get("summary"))
        and set(review.get("review_standard", [])) >= {"review-v1", "catalog-review-v1"}
        and len(rows) == len(wanted) and {r.get("id") for r in rows} == wanted
        and all(r.get("status") in STATES and r.get("evidence") and r.get("reason") for r in rows)
    )
    return rows, complete


def summary(output: dict) -> str:
    if not output:
        return "无正式业务输出"
    facts = output.get("facts")
    detail = (f"{facts.get('sku_count')}SKU / {facts.get('min_price')}～"
              f"{facts.get('max_price')}元") if facts else (
                  f"返回{output.get('returned_count')}项 / {output.get('reason_code')}")
    return f"{output.get('result_type')} / {detail}"


def render(folder) -> None:
    data = folder / "evidence"
    suite = read_json(data / "cases.json")
    manifest = read_json(data / "environment.json")
    if suite.get("type") not in GROUPS:
        raise SystemExit("Use the pilot checker for price-lookup-pilot-1")
    executions = read_jsonl(data / "executions.jsonl")
    reviews = read_jsonl(folder / "checks/semantic.jsonl")
    archive_analysis(folder)
    integrity = all(sha(data / file) == digest
                    for file, digest in manifest["frozen_sha256"].items())
    source_stable = bool(manifest.get("code_after")) and (
        manifest["code_after"]["fingerprints"] == manifest["code"]["fingerprints"])
    baseline = read_json(data / "baseline.json")["prepared"]
    preflight = read_json(data / "preflight.json") if (data / "preflight.json").exists() else {}
    automatic, table, problems, counts = [], [], [], Counter()
    for case in suite["cases"]:
        case_id = case["id"]
        attempts = [r for r in executions if r.get("case_id") == case_id]
        if not attempts:
            reason = (manifest.get("execution_started", {}).get("skipped", {}).get(case_id)
                      or case["readiness"]["reason"] or manifest.get("execution_error")
                      or "尚无正式调用记录")
            # Data drift makes the intended conclusion indeterminate, not a pass.
            uncertain = reason == "data_changed_or_unverifiable" or (
                reason == "尚无正式调用记录" and manifest.get("execution_started"))
            final = "无法判定" if uncertain else "未执行"
            counts[final] += 1
            table.append(f"|{case_id}：{compact(case['input'])}|{case['intent']}|{reason}|"
                         f"未执行|未执行|{final}|")
            automatic.append({"case_id": case_id, "attempt": None, "final": final,
                              "reason": reason, "hard_checks": [], "trace_checks": []})
            continue
        for execution in attempts:  # Never hide duplicate attempts by keying a dict on case_id.
            hard, traces, evidence, semantic = [], [], {}, []
            messages = []
            if execution.get("batch_id") != folder.name:
                messages.append("执行记录批次与当前目录不符，不能拼接其他批次证据")
            raw_path = data / "traces" / f"{case_id}.json"
            response = read_json(raw_path) if raw_path.exists() else {}
            pre = preflight.get("before_execution", {}).get(case_id, {})
            data_valid = (baseline[case_id]["eligible"] and pre.get("eligible")
                          and baseline[case_id].get("oracle") == pre.get("oracle"))
            if not execution.get("result"):
                messages.append("实验调用未取得正式结果；查看harness_error，不能归为商品无匹配")
            elif not integrity or not data_valid:
                messages.append("预期快照或数据依据不再有效，未据此强行判业务成败")
            else:
                hard = hard_checks(suite, case, execution)
            try:
                traces, evidence = trace_rows(case, execution, response)
            except (KeyError, TypeError, ValueError) as exc:
                traces = [{"field": "trace_read", "status": "无法判定",
                           "actual": type(exc).__name__, "expected": "完整可解析Trace"}]
            semantic, complete = semantic_rows(suite, case, execution, reviews)
            if not complete:
                messages.append("必要语义判读缺失、未完整填写或关联不唯一")
            if not source_stable:
                messages.append("执行期间代码指纹变化，不能认定版本证据完整")
            if len(attempts) != 1:
                messages.append("同例多次记录，全部保留；当前Trace文件不能证明每次尝试")
            failed = any(row.get("status") == "失败" for row in hard + semantic)
            final = "失败" if failed else "通过" if (
                hard and complete and not messages and status(hard + traces + semantic) == "通过"
            ) else "无法判定"
            counts[final] += 1
            automatic.append({"case_id": case_id, "attempt": execution.get("attempt"),
                              "trace_id": execution.get("trace_id"), "final": final,
                              "hard_checks": hard, "trace_checks": traces,
                              "semantic_complete": complete, "notes": messages,
                              "trace_key_information": {
                                  "models": [{k: m[k] for k in (
                                      "span_id", "stage", "attempt", "output")}
                                             for m in evidence.get("models", [])],
                                  "validations": [{k: v[k] for k in (
                                      "span_id", "validation", "outcome", "model_span_id")}
                                                  for v in evidence.get("validations", [])],
                                  "events": evidence.get("events", []),
                                  "raw_file": f"evidence/traces/{case_id}.json",
                              }})
            failed_names = [r.get("field", r.get("id")) for r in hard + traces + semantic
                            if r.get("status") != "通过"]
            if final != "通过":
                detail = "；".join(messages + ["未通过项：" + ", ".join(failed_names[:5])])
                problems.append(f"- {case_id}（attempt={execution.get('attempt')}）：{detail}。"
                                "详见程序/语义明细。")
            matched_reviews = [r for r in reviews if r.get("case_id") == case_id
                               and r.get("batch_id") == execution["batch_id"]
                               and r.get("attempt") == execution.get("attempt")
                               and r.get("trace_id") == execution.get("trace_id")]
            semantic_summary = matched_reviews[0].get("summary", "") if len(matched_reviews) == 1 else ""
            for review in matched_reviews:
                for note in review.get("notes", []):
                    problems.append(f"- {case_id} · 复检观察：{note}")
            trace_id = execution.get("trace_id") or "未记录"
            link = f"[{case_id}](evidence/traces/{case_id}.json)" if raw_path.exists() else case_id
            actual = summary((execution.get("result") or {}).get("output") or {})
            semantic_status = status(semantic, empty="无法判定")
            if not complete and semantic_status != "失败":
                semantic_status = "无法判定"
            table.append(f"|{link}：{compact(case['input'])}<br>Trace `{trace_id}`|"
                         f"{case['intent']}|{actual}|{status(hard, empty='无法判定')}|"
                         f"{semantic_status} {compact(semantic_summary)}|{final}|")
    write_jsonl(folder / "checks/automatic.jsonl", automatic)
    totals = "，".join(f"{key} {counts[key]}" for key in ("通过", "失败", "无法判定", "未执行"))
    references = [("原始调用", "evidence/executions.jsonl"), ("用例", "evidence/cases.json"),
                  ("独立数据", "evidence/baseline.json"), ("运行前复核", "evidence/preflight.json"),
                  ("程序明细", "checks/automatic.jsonl"), ("语义明细", "checks/semantic.jsonl"),
                  ("环境", "evidence/environment.json")]
    reference_line = "按需追查：" + "、".join(
        f"[{label}]({path})" if (folder / path).exists() else f"{label}（未生成）"
        for label, path in references)
    lines = [f"# catalog_search · {suite['type']}", "", totals + "。", "",
             (f"批次 `{folder.name}`；用例 `{suite['version']}`；模型 `{manifest['model']}`；"
              f"代码 `{manifest['code']['head'][:8]}`（工作区明细见environment）。"),
             f"正式调用记录 {len(executions)} 条；仅单工具；未执行聊天/编排/前端。", "",
             "|用例、输入、Trace|关键预期|实际摘要|程序|语义|最终|",
             "|---|---|---|---|---|---|", *table, "", "问题与限制：", "",
             *(problems or ["- 表中未执行/无法判定项不计通过；通过仅代表已冻结标准。"]),
             "- Trace链接指向本地原始记录；没有核验Phoenix页面URL时不编造页面链接。",
             "- 数据缺口和隔离数据需求见用例data_requirement；本脚本不会创建或修改商品。", "",
             reference_line + "。"]
    (folder / "report.md").write_text("\n".join(lines) + "\n")
    print(totals)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fetch", metavar="BATCH")
    mode.add_argument("--render", metavar="BATCH")
    args = parser.parse_args()
    folder = batch_path(args.fetch or args.render)
    if args.fetch:
        if os.environ.get("PC_AGENT_OFFLINE_TEST"):
            raise SystemExit("Phoenix fetch is forbidden in the offline runner")
        data = folder / "evidence"
        manifest = read_json(data / "environment.json")
        for row in read_jsonl(data / "executions.jsonl"):
            target = data / "traces" / f"{row['case_id']}.json"
            if not row.get("trace_id") or target.exists():
                continue
            url = "http://127.0.0.1:6006/v1/projects/pc-ecommerce-agent-local/spans?" + (
                urllib.parse.urlencode({"trace_id": row["trace_id"]}))
            record = {"at": now(), "url": url}
            try:
                response = get_json(url)
                record["received"] = len(response.get("data", []))
                if response.get("data"):
                    write_json(target, response)
            except (OSError, ValueError) as exc:
                record["error_type"] = type(exc).__name__
            manifest.setdefault("trace_fetch", {}).setdefault(row["case_id"], []).append(record)
        write_json(data / "environment.json", manifest)
    render(folder)


if __name__ == "__main__":
    main()
