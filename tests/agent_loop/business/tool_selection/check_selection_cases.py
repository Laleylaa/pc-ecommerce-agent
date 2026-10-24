"""Offline evidence checks and report rendering; never imports application code.

Independent LLM review is a separate step described in REVIEW.md/review_prompt.md.
An automatic pass cannot fill in a missing semantic verdict.
"""

import argparse
import json
import shutil
from collections import Counter
from datetime import datetime
from uuid import uuid4

from run_selection_cases import MEASUREMENT, batch_path, now, read, sha, write

PASS, FAIL, UNKNOWN, NOT_RUN = "通过", "失败", "无法判定", "未执行"
CONTROLS = {"register_goals", "query_complete"}


def jsonl(path):
    if not path.exists():
        return []
    # Interrupted final writes remain visible; do not silently skip malformed lines.
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def archive(batch):
    paths = [batch / name for name in (
        "report.md", "checks/automatic.jsonl", "checks/semantic.jsonl",
    ) if (batch / name).exists()]
    if not paths:
        return
    dest = batch / "checks/history" / (now().replace(":", "-") + "-" + uuid4().hex[:6])
    dest.mkdir(parents=True)
    for path in paths:
        shutil.copy2(path, dest / path.name)
    write(dest / "manifest.json", {str(p.relative_to(batch)): sha(p) for p in paths})


def initial_response(events):
    """One completed first response, with no later feedback round to select from."""
    responses = [e for e in events if e["event"] == "response"
                 and 200 <= e.get("status_code", 0) < 300
                 and e.get("body", {}).get("choices")]
    return responses[0] if len(responses) == 1 else None


def proposals(events):
    response = initial_response(events)
    if response is None:
        return []
    calls = []
    for call in response["body"]["choices"][0].get("message", {}).get("tool_calls") or []:
        fn = call.get("function") or {}
        name = fn.get("name", "unknown")
        if name in CONTROLS:
            continue
        try:
            arguments = json.loads(fn.get("arguments", ""))
        except (ValueError, TypeError):
            arguments = None
        calls.append({"name": name, "input": arguments,
                      "request_id": response["request_id"], "tool_call_id": call.get("id")})
    return calls


def automatic(case, execution, events):
    calls = proposals(events)
    actual = sorted({call["name"] for call in calls})
    expected = case["expected_tool_sets"]
    response = initial_response(events)
    boundary_ok = (
        execution.get("state") == "response_received"
        and execution.get("domain_executions") == 0
        and execution.get("control_executions") == 0
        and response is not None
        and all(m.get("role") in {"system", "user"}
                for e in events if e["event"] == "request"
                for m in e.get("body", {}).get("messages", []))
    )
    if not boundary_ok:
        verdict = UNKNOWN
    elif actual in expected:
        verdict = PASS
    elif not actual:
        # Registration-only / text-only is not an observed business tool choice.
        # Do not generate control feedback just to force a second decision.
        verdict = UNKNOWN
    else:
        verdict = FAIL
    return [
        {"id": "selection_boundary", "expected": "one response; zero tools/feedback",
         "actual": boundary_ok, "status": PASS if boundary_ok else UNKNOWN},
        {"id": "initial_tool_set", "expected": expected, "actual": actual, "status": verdict},
    ], calls


def combine(statuses):
    if FAIL in statuses:
        return FAIL
    if not statuses or UNKNOWN in statuses:
        return UNKNOWN
    return PASS


def semantic_status(row, case, execution, batch):
    if row is None:
        return UNKNOWN, "待独立LLM语义复检"
    required = set(case["semantic_checks"])
    identity = (row.get("batch_id"), row.get("case_id"), row.get("attempt"))
    if identity != (batch.name, case["id"], execution["attempt"]):
        return UNKNOWN, "语义记录关联不一致"
    if (row.get("trace_sha256") != execution.get("trace_sha256")
            or row.get("reviewer_kind") not in {"llm", "human"}
            or not row.get("reviewer") or not row.get("summary")
            or (row.get("reviewer_kind") == "llm" and not row.get("model"))):
        return UNKNOWN, "缺判读者、模型或本次证据指纹"
    if not {"review-v1", "tool-selection-review-v3"} <= set(row.get("review_standard", [])):
        return UNKNOWN, "缺判读准则版本"
    try:
        reviewed = datetime.fromisoformat(row["reviewed_at"])
        if reviewed.tzinfo is None:
            return UNKNOWN, "判读时间缺时区"
    except (KeyError, ValueError, TypeError):
        return UNKNOWN, "判读时间无效"
    items = row.get("checks", [])
    if len(items) != len(required) or {x.get("id") for x in items} != required:
        return UNKNOWN, "必要语义项缺失或重复"
    for item in items:
        if (item.get("status") not in {PASS, FAIL, UNKNOWN} or not item.get("reason")
                or not item.get("evidence")):
            return UNKNOWN, "语义项缺结论、理由或证据"
        for reference in item["evidence"]:
            file = (batch / reference.split("#", 1)[0]).resolve()
            if not file.is_relative_to(batch.resolve()) or not file.is_file():
                return UNKNOWN, "语义引用文件缺失或越出当前批次"
    return combine([x["status"] for x in items]), row["summary"]


def render(batch):
    env = read(batch / "evidence/environment.json")
    suite = read(batch / "evidence/cases.json")
    if (env.get("measurement") != MEASUREMENT
            or suite.get("measurement") != MEASUREMENT):
        raise ValueError("Legacy feedback-loop reports cannot be rescored as tool selection")
    cases = {c["id"]: c for c in suite["cases"]}
    frozen_ok = all(sha(batch / "evidence" / n) == h
                    for n, h in env["frozen_hashes"].items())
    if not frozen_ok:
        raise ValueError("Frozen evidence changed; preserve the batch and investigate")
    executions = {}
    for row in jsonl(batch / "evidence/executions.jsonl"):
        key = (row["case_id"], row["attempt"])
        if key in executions and executions[key]["state"] != "started":
            raise ValueError("Duplicate terminal execution; do not collapse attempts")
        executions[key] = row
    semantics = {}
    for row in jsonl(batch / "checks/semantic.jsonl"):
        key = (row["case_id"], row["attempt"])
        if key in semantics:
            raise ValueError("Duplicate semantic verdict; archive old review first")
        semantics[key] = row
    archive(batch)
    checks_dir = batch / "checks"
    checks_dir.mkdir(exist_ok=True)
    automatic_rows, table, packets = [], [], []
    for planned in env["planned_attempts"]:
        cid, attempt = planned["case_id"], planned["attempt"]
        case = cases[cid]
        execution = executions.get((cid, attempt))
        trace_rel = f"evidence/traces/{cid}-{attempt}.jsonl"
        events = jsonl(batch / trace_rel)
        did_request = any(x["event"] == "request" for x in events)
        checks, calls = [], []
        auto, semantic, status = UNKNOWN, UNKNOWN, UNKNOWN
        summary = "缺少完成记录或必要模型调用证据"
        if execution is None and not events:
            status, summary = NOT_RUN, "本批没有调用记录"
        elif execution and execution.get("http_requests") == 0 and not did_request:
            status, summary = NOT_RUN, "模型调用前准备失败"
        elif execution:
            checks, calls = automatic(case, execution, events)
            trace = batch / trace_rel
            responses = {e["request_id"] for e in events if e["event"] in {
                "response", "transport_error",
            }}
            requests = {e["request_id"] for e in events if e["event"] == "request"}
            evidence_ok = (frozen_ok and did_request and trace.exists()
                           and execution.get("trace_sha256") == sha(trace)
                           and requests == responses)
            checks.append({"id": "evidence_complete", "expected": True,
                           "actual": evidence_ok, "status": PASS if evidence_ok else UNKNOWN})
            auto = combine([x["status"] for x in checks])
            semantic, summary = semantic_status(
                semantics.get((cid, attempt)), case, execution, batch,
            )
            status = combine([auto, semantic])
        automatic_rows.append({
            "batch_id": batch.name, "case_id": cid, "attempt": attempt,
            "trace_file": trace_rel, "checks": checks, "automatic_status": auto,
            "semantic_status": semantic, "status": status, "summary": summary,
            "initial_selection": calls,
            "proposals": calls,
        })
        table.append((cid, attempt, case, auto, semantic, status, summary, calls))
        if did_request:
            packets.append({
                "batch_id": batch.name, "case_id": cid, "attempt": attempt,
                "case": case, "execution": execution, "trace_file": trace_rel,
                "trace_sha256": sha(batch / trace_rel),
                "contexts": "evidence/contexts.json",
                "review_prompt": "evidence/review_prompt.md",
            })
    with (checks_dir / "automatic.jsonl").open("w") as stream:
        for row in automatic_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    write(checks_dir / "review_packets.json", packets)

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    counts = Counter(row["status"] for row in automatic_rows)
    lines = [
        "# 工具选择模块测试报告", "", "## 本批结论", "",
        f"批次 `{batch.name}`；分类 `{env['group']}`；模型 `{env.get('model')}`。",
        f"冻结版本 `{suite['version']}`；HEAD `{env['head']}`，工作区源码另有快照及指纹。",
        f"共 {len(cases)} 条用例、{len(table)} 次计划尝试："
        + "，".join(f"{s} {counts[s]}" for s in (PASS, FAIL, UNKNOWN, NOT_RUN)) + "。",
        "只验证用户输入对应的首次工具选择；未执行业务/控制工具，未返回任何工具反馈。",
        "不以参数值、调用次数、控制协议或后续恢复判定工具选择；语义复检不是人工验收。",
        "", "## 逐次结果", "",
        "| 用例/尝试 | 用户输入 | 允许工具集合 → 首次选择 | 程序 | 语义 | 结论 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cid, attempt, case, auto, semantic, status, summary, calls in table:
        expected = case["expected_tool_sets"]
        actual = sorted({c["name"] for c in calls})
        lines.append("| " + " | ".join(map(cell, (
            f"{cid}/{attempt}", case["input"]["message"],
            f"{expected} → {actual}",
            auto, semantic, status, summary,
        ))) + " |")
    lines.extend(["", "## 问题与限制", "",
                  "未执行和无法判定均不计通过；所有重复尝试分别列出，不挑选最好一次。",
                  "语义复检只判断工具职责匹配及混合业务目的的工具覆盖；参数仅辅助识别用途。",
                  "没有业务结果、恢复过程、领域内部Trace、数据库或最终回答证据。",
                  "只登记/只输出文字而未提出必要业务工具时记无法判定，不执行控制反馈续问。",
                  "未验证用例以外的表达、领域执行、回答生成及聊天端到端效果。", ""])
    for row in automatic_rows:
        if row["status"] == PASS:
            continue
        failed = [x["id"] for x in row["checks"] if x["status"] != PASS]
        lines.append(f"- {row['case_id']}/{row['attempt']}：{row['status']}；"
                     f"{row['summary']}；程序未通过项：{', '.join(failed) or '无/尚未执行'}。"
                     f"证据：`{row['trace_file']}`，`checks/automatic.jsonl`。")
    (batch / "report.md").write_text("\n".join(lines) + "\n")
    print(f"Rendered {batch / 'report.md'}; no model or database requests")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--render", metavar="BATCH")
    actions.add_argument("--archive", metavar="BATCH")
    args = parser.parse_args()
    if args.archive:
        archive(batch_path(args.archive))
    else:
        render(batch_path(args.render))
