"""Fetch Phoenix evidence and check the frozen basic-price suite; never calls tools/models.

--fetch BATCH reads only local Phoenix. --render BATCH is filesystem-only and
merges separately authored checks/semantic.jsonl. Neither manufactures semantic verdicts.
"""

import argparse
import copy
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from run_price_lookup import (
    batch_path,
    now,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
)


def get_json(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.load(response)


def path_value(value, path):
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return {"missing": path}
        value = value[key]
    return value


def compare(actual, mode, expected):
    try:
        if mode == "eq":
            return actual == expected
        if mode == "set_eq":
            return isinstance(actual, list) and set(actual) == set(expected)
        if mode == "decimal_eq":
            return Decimal(str(actual)) == Decimal(expected)
        if mode == "one_of":
            return actual in expected
        if mode == "contains":
            return expected in actual
    except (TypeError, ValueError, ArithmeticError):
        return False
    raise ValueError("Unknown comparison: " + mode)


def decode(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


def extract(spans):
    evidence = {"models": [], "validations": [], "events": [], "tools": [], "roots": []}
    for span in sorted(spans, key=lambda s: s["start_time"]):
        attrs = span.get("attributes", {})
        row = {"span_id": span["context"]["span_id"], "parent_id": span.get("parent_id"),
               "trace_id": span["context"]["trace_id"], "name": span["name"],
               "input": decode(attrs.get("input.value")),
               "output": decode(attrs.get("output.value"))}
        if not span.get("parent_id"):
            evidence["roots"].append(row | {"batch_id": attrs.get("batch_id"),
                                            "case_id": attrs.get("case_id"),
                                            "attempt": attrs.get("attempt")})
        if span.get("span_kind") == "LLM":
            evidence["models"].append(row | {
                "stage": attrs.get("stage"), "attempt": attrs.get("attempt"),
                "messages": {k: v for k, v in attrs.items()
                             if k.startswith(("llm.input_messages.", "llm.output_messages."))},
                "input_truncated": attrs.get("trace.input.truncated"),
                "output_truncated": attrs.get("trace.output.truncated")})
        if span.get("span_kind") == "TOOL":
            evidence["tools"].append(row)
        if attrs.get("record_type") == "program_validation":
            evidence["validations"].append(row | {
                "validation": attrs.get("validation_name"),
                "outcome": attrs.get("validation_outcome"),
                "model_span_id": attrs.get("model_span_id")})
        for event in span.get("events", []):
            event_attrs = event.get("attributes", {})
            evidence["events"].append({"span_id": row["span_id"], "name": event["name"],
                                       "attributes": {k: decode(v) for k, v in event_attrs.items()}})
    return evidence


def hard_checks(suite, case, execution):
    value = copy.deepcopy(execution.get("result", {}))
    output = value.get("output") or {}
    if output:
        output["diagnostic_codes"] = [d["code"] for d in output.get("diagnostics", [])]
    checks = []
    for path, mode, expected in suite["common_hard"] + case["hard"]:
        actual = path_value(value, path)
        checks.append({"field": path, "comparison": mode, "expected": expected,
                       "actual": actual, "status": "通过" if compare(actual, mode, expected)
                       else "失败"})
    conditions = output.get("query_plan", {}).get("conditions")
    allowed = isinstance(conditions, list) and all(
        case["condition_policy"] == "none_or_keychron_brand"
        and c.get("key") == "brand" and c.get("operator") in {"eq", "in"}
        and c.get("strength") == "requirement"
        and [str(v).casefold() for v in c.get("values", [])] == ["keychron"]
        for c in conditions)
    checks.append({"field": "output.query_plan.conditions", "comparison": "allowed_forms",
                   "expected": case["condition_policy"], "actual": conditions,
                   "status": "通过" if allowed else "失败"})
    return checks


def trace_checks(suite, case, execution, evidence, raw):
    checks = []
    def check(label, expected, actual, passed):
        checks.append({"field": label, "expected": expected, "actual": actual,
                       "status": "通过" if passed else "无法判定"})
    roots, tools = evidence["roots"], evidence["tools"]
    model_stages = [m["stage"] for m in evidence["models"]]
    check("root_correlation", [execution["batch_id"], case["id"], 1],
          [[r.get("batch_id"), r.get("case_id"), r.get("attempt")] for r in roots],
          len(roots) == 1 and roots[0].get("batch_id") == execution["batch_id"]
          and roots[0].get("case_id") == case["id"] and roots[0].get("attempt") == 1)
    check("actual_model_stages", ["tool_planning", "tool_review"], model_stages,
          {"tool_planning", "tool_review"}.issubset(model_stages))
    check("HTTP_count", execution["usage"]["http_attempts"], len(evidence["models"]),
          len(evidence["models"]) == execution["usage"]["http_attempts"])
    ids = {s["context"]["span_id"] for s in raw}
    check("trace_tree", "同 Trace、完整父子关系、一个工具、模型及校验位于工具下",
          len(raw), bool(raw) and len(tools) == 1 and len(roots) == 1
          and tools[0]["parent_id"] == roots[0]["span_id"]
          and all(s["context"]["trace_id"] == execution["trace_id"]
                  and (not s.get("parent_id") or s["parent_id"] in ids) for s in raw)
          and all(s["parent_id"] == tools[0]["span_id"]
                  for s in evidence["models"] + evidence["validations"]))
    for name in suite["trace_requirements"]["checks"]:
        entries = [v for v in evidence["validations"] if v["validation"] == name]
        check(name, "存在 accepted 节点（拒绝和修正仍完整保留）",
              [v["outcome"] for v in entries], any(v["outcome"] == "accepted" for v in entries))
    event_names = [e["name"] for e in evidence["events"]]
    check("program_events", suite["trace_requirements"]["events"], event_names,
          set(suite["trace_requirements"]["events"]).issubset(event_names))
    check("tool_input", case["input"], tools[0]["input"] if tools else None,
          len(tools) == 1 and tools[0]["input"] == case["input"])
    check("tool_output", "等于保存的正式工具返回值", "见原始 Trace 的 TOOL output.value",
          len(tools) == 1 and tools[0]["output"] == execution.get("result"))
    output_checks = [v for v in evidence["validations"] if v["validation"] == "工具输出校验"]
    check("pre_validation_output", "等于正式返回的 output", "见 validations/input",
          len(output_checks) == 1
          and output_checks[0]["input"] == execution.get("result", {}).get("output"))
    truncated = [s["name"] for s in raw if any(
        (k.endswith((".truncated", ".incomplete")) and v is True)
        or (isinstance(v, str) and ("已截断" in v or '"capture_status": "unavailable"' in v))
        for k, v in s.get("attributes", {}).items())]
    check("trace_not_truncated", [], truncated, not truncated and bool(raw))
    return checks


def compact(value):
    return json.dumps(value, ensure_ascii=False).replace("|", "\\|").replace("\n", " ")


def status(rows, *, empty="待复检"):
    if not rows:
        return empty
    if any(r["status"] == "失败" for r in rows):
        return "失败"
    return "通过" if all(r["status"] == "通过" for r in rows) else "无法判定"


def price_summary(facts):
    if not facts:
        return "无价格事实"
    target = facts.get("target", {})
    return (f"{str(target.get('kind', '?')).upper()}{target.get('id', '?')} / "
            f"{facts.get('sku_count', '?')} SKU / "
            f"{facts.get('min_price', '?')}～{facts.get('max_price', '?')}元")


def render(folder):
    data_dir = folder / "evidence"
    suite = read_json(data_dir / "cases.json")
    manifest = read_json(data_dir / "environment.json")
    executions = {r["case_id"]: r for r in read_jsonl(data_dir / "executions.jsonl")}
    reviews = {r["case_id"]: r for r in read_jsonl(folder / "checks/semantic.jsonl")}
    automatic, rows, problems = [], [], []
    for case in suite["cases"]:
        case_id = case["id"]
        execution = executions.get(case_id)
        review = reviews.get(case_id, {})
        semantic = review.get("checks", [])
        hard, traces, raw = [], [], []
        final = "未执行"
        if execution:
            trace_path = data_dir / "traces" / f"{case_id}.json"
            raw = read_json(trace_path).get("data", []) if trace_path.exists() else []
            hard = hard_checks(suite, case, execution)
            traces = trace_checks(suite, case, execution, extract(raw), raw)
            semantic_complete = (
                review.get("trace_id") == execution["trace_id"]
                and {r["id"] for r in semantic} == {r["id"] for r in suite["common_semantic"]}
                and all(r.get("evidence") and r.get("reason") for r in semantic))
            failed = any(r["status"] == "失败" for r in hard + semantic)
            passed = semantic_complete and status(hard + traces + semantic) == "通过"
            final = "失败" if failed else "通过" if passed else "无法判定"
            automatic.append({"case_id": case_id, "attempt": execution["attempt"],
                              "trace_id": execution["trace_id"],
                              "hard_checks": hard, "trace_checks": traces})
            for label, entries in (("程序", hard), ("Trace证据", traces), ("语义", semantic)):
                for entry in entries:
                    if entry["status"] != "通过":
                        problems.append(f"- {case_id} · {label} · {entry['status']}："
                                        f"{entry.get('field', entry.get('id'))}；"
                                        + (entry.get("reason") or
                                           f"预期 {compact(entry.get('expected'))}，"
                                           f"实际 {compact(entry.get('actual'))}"))
            if not semantic_complete:
                problems.append(f"- {case_id}：本次 Trace 的必要语义复检尚未完成，不能判通过。")
        else:
            reason = manifest.get("execution_started", {}).get("preflight_reason")
            problems.append(f"- {case_id}：未执行（{reason or '尚无正式调用记录'}）。")
        for note in review.get("notes", []):
            problems.append(f"- {case_id} · 复检观察：{note}")
        expected = {p: v for p, _, v in case["hard"]}
        expected_price = price_summary({
            "target": expected["output.facts.target"],
            "sku_count": expected["output.facts.sku_count"],
            "min_price": expected["output.facts.min_price"],
            "max_price": expected["output.facts.max_price"]})
        output = ((execution or {}).get("result") or {}).get("output") or {}
        actual_price = price_summary(output.get("facts")) if execution else "未执行"
        link = f"[{case_id}](evidence/traces/{case_id}.json)" if raw else case_id
        query = case["input"]["query"]
        if "limit" in case["input"]:
            query += f"（参数 limit={case['input']['limit']}）"
        semantic_text = status(semantic) + ("：" + review["summary"] if review.get("summary") else "")
        rows.append({"final": final, "line":
                     f"|{link}：{query}|{expected_price} → {actual_price}|"
                     f"{status(hard, empty='未执行')}|{semantic_text}|{final}|"})
    write_jsonl(folder / "checks/automatic.jsonl", automatic)
    counts = "，".join(f"{name} {sum(r['final'] == name for r in rows)}" for name in (
        "通过", "失败", "无法判定", "未执行"))
    http_count = sum(r["usage"]["http_attempts"] for r in executions.values())
    trace_ok = sum(status(r["trace_checks"]) == "通过" for r in automatic)
    lines = ["# 基础价格查询 · 测试报告", "", "## 本批结论", "",
             (f"{counts}。执行 {len(executions)} 条原有用例，真实模型请求 {http_count} 次；"
              f"Trace证据检查通过 {trace_ok}/{len(executions)}。"),
             (f"模型 `{manifest['model']}`；用例 `{suite['version']}`；"
              f"代码 `{manifest['code']['head'][:8]}`（含原有未提交修改）。"),
             f"批次：`{folder.name}`。", "", "## 逐例结果", "",
             "|用例及输入|关键预期 → 实际结果|程序校验|我的语义复检|最终|",
             "|---|---|---|---|---|", *[r["line"] for r in rows], "",
             "## 问题与限制", "", *(problems or ["- 本批未发现违反预先标注的行为。"]),
             "- 仅这3条基础价格 lookup；不代表 search/variants、复杂条件、超过20 SKU或失败分支已验收。",
             "- 商城数据库只读；未执行聊天、编排、回答或前端；Trace API读回不等于页面验收。", "",
             ("追查时再看：[原始调用](evidence/executions.jsonl)、"
             "[数据依据](evidence/baseline.json)、[用例标注](evidence/cases.json)、"
             "[程序明细](checks/automatic.jsonl)、[语义明细](checks/semantic.jsonl)、"
             "[环境与运行记录](evidence/environment.json)。用例编号链接到原始Trace。")]
    (folder / "report.md").write_text("\n".join(lines) + "\n")
    print(counts)


def main(args):
    folder = batch_path(args.fetch or args.render)
    data_dir = folder / "evidence"
    if args.fetch:
        if os.environ.get("PC_AGENT_OFFLINE_TEST"):
            raise SystemExit("Do not fetch Phoenix inside the offline test runner")
        manifest = read_json(data_dir / "environment.json")
        for execution in read_jsonl(data_dir / "executions.jsonl"):
            trace_path = data_dir / "traces" / f"{execution['case_id']}.json"
            if not execution["trace_id"] or trace_path.exists():
                continue
            url = "http://127.0.0.1:6006/v1/projects/pc-ecommerce-agent-local/spans?" + (
                urllib.parse.urlencode({"trace_id": execution["trace_id"]}))
            attempts = []
            for _ in range(3):
                try:
                    response = get_json(url)
                    attempts.append({"at": now(), "received": len(response.get("data", []))})
                    if response.get("data"):
                        write_json(trace_path, response)
                        break
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    attempts.append({"at": now(), "error_type": type(exc).__name__})
                time.sleep(0.5)
            manifest.setdefault("trace_fetch", {}).setdefault(
                execution["case_id"], []).append({"url": url, "attempts": attempts})
        write_json(data_dir / "environment.json", manifest)
    render(folder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--fetch", metavar="BATCH")
    modes.add_argument("--render", metavar="BATCH")
    main(parser.parse_args())
