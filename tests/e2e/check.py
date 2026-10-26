"""Offline hard checks and evidence-backed semantic review; never runs the application."""

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

try:
    from .run import verify_batch
    from .suite import VERSION, check_value, digest, now, read, write
except ImportError:
    from run import verify_batch
    from suite import VERSION, check_value, digest, now, read, write


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def semantic_valid(row: dict, batch: Path, fingerprint: str) -> bool:
    reviewer = row.get("reviewer", {})
    if (
        row.get("execution_sha256") != fingerprint
        or row.get("version") != VERSION
        or reviewer.get("kind") not in {"human", "llm"}
        or not reviewer.get("name")
        or not row.get("reviewed_at")
        or not row.get("reason")
        or not row.get("evidence")
    ):
        return False
    for reference in row["evidence"]:
        filename, separator, locator = reference.partition("#")
        path = (batch / filename).resolve()
        if (
            not path.is_relative_to((batch / "evidence").resolve())
            or not path.is_file()
            or not separator
            or not locator
        ):
            return False
    return row.get("status") in {"pass", "fail", "unknown"}


def combined(statuses: list[str]) -> str:
    if "fail" in statuses:
        return "fail"
    if statuses and all(status == "not_run" for status in statuses):
        return "not_run"
    if not statuses or any(status != "pass" for status in statuses):
        return "unknown"
    return "pass"


def corrected_path(batch: Path, case_id: str, turn: int, criterion: dict) -> dict:
    """Audited annotation repair only; preserve frozen input and original expected values."""
    path = batch / "checks/path_corrections.json"
    if not path.exists():
        return criterion
    document = read(path)
    if document["frozen_sha256"] != digest(batch / "evidence/frozen.json"):
        raise ValueError("annotation correction targets different frozen evidence")
    matches = [r for r in document["corrections"]
               if (r["case_id"], r["turn"], r["original"]["id"])
               == (case_id, turn, criterion["id"])]
    if not matches:
        return criterion
    if len(matches) != 1 or matches[0]["original"] != criterion:
        raise ValueError("ambiguous or stale annotation correction")
    row = matches[0]
    if not row["reason"] or not row["schema_evidence"] or not document["revision"]:
        raise ValueError("annotation correction needs revision, reason and independent schema")
    filename, separator, locator = row["schema_evidence"].partition("#")
    schema = (batch / filename).resolve()
    if not (
        separator and locator and schema.is_file()
        and schema.is_relative_to((batch / "evidence/source").resolve())
    ):
        raise ValueError("correction must cite the frozen source schema")
    return {**criterion, "path": row["corrected_path"], "annotation_revision": document["revision"]}


def history_checks(events: list[dict], responses: list[dict], turns: list[dict]) -> list[dict]:
    if len(responses) != len(turns):
        return [
            {
                "id": "history",
                "status": "unknown",
                "reason": "incomplete conversation; retain history without assuming completion",
            }
        ]
    history = [event for event in events if event["event"] == "history"]
    if not history:
        return [{"id": "history", "status": "unknown", "reason": "history unavailable"}]
    item = history[-1]
    try:
        body = item["body"]
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        users = [m for m in body["messages"] if m["role"] == "user"]
        valid = item["http_status"] == 200 and len(assistants) == len(responses)
        valid &= len(users) == len(turns)
        valid &= [m["content"] for m in users] == [t["message"] for t in turns]
        valid &= all(body["id"] == response["conversation_id"] for response in responses)
        for message, response in zip(assistants, responses, strict=True):
            valid &= message["content"] == response["answer"]
            for field in ("products", "order", "orders", "evidence", "suggested_actions"):
                valid &= message["metadata"][field] == response[field]
        return [
            {
                "id": "history",
                "status": "pass" if valid else "fail",
                "reason": "compare saved messages/cards with actual requests/responses",
            }
        ]
    except (KeyError, TypeError, ValueError):
        return [{"id": "history", "status": "fail", "reason": "incomplete history/response"}]


def render(batch: Path) -> dict:
    frozen = verify_batch(batch)
    executions = batch / "evidence/executions.jsonl"
    events = jsonl(executions)
    fingerprint = digest(executions) if executions.exists() else "not-run"
    finished = batch / "evidence/run_finished.json"
    if finished.exists():
        expected = read(finished)["executions_sha256"]
        if expected != (fingerprint if executions.exists() else None):
            raise ValueError("execution evidence changed after run finished")
    checks_dir = batch / "checks"
    reviews = jsonl(checks_dir / "semantic.jsonl")
    review_map = {}
    for row in reviews:
        key = (row["case_id"], row["attempt"], row["turn"], row["criterion"])
        if key in review_map:
            raise ValueError("duplicate semantic review; archive previous review before replacing")
        review_map[key] = row
    automatic, templates, packets, results = [], [], [], []
    for case in frozen["cases"]:
        for attempt in range(1, frozen["repeat"] + 1):
            matching = [
                e for e in events if e.get("case_id") == case["id"] and e.get("attempt") == attempt
            ]
            statuses, responses, reasons = [], [], []
            conversation_id = None
            for number, turn in enumerate(case["turns"], 1):
                key = {"case_id": case["id"], "attempt": attempt, "turn": number}
                turn_events = [e for e in matching if e.get("turn") == number]
                response_events = [e for e in turn_events if e["event"] == "response"]
                started = any(e["event"] == "started" for e in turn_events)
                hard = []
                if response_events:
                    event = response_events[-1]
                    body = event["body"]
                    ok = event["http_status"] == 200 and isinstance(body, dict)
                    hard.append(
                        {
                            "id": "http",
                            "status": "pass" if ok else "fail",
                            "actual": event["http_status"],
                        }
                    )
                    if ok:
                        received_id = body.get("conversation_id")
                        valid = type(received_id) is int and received_id > 0
                        valid &= conversation_id is None or conversation_id == received_id
                        hard.append({"id": "conversation", "status": "pass" if valid else "fail"})
                        conversation_id = received_id
                        hard.extend(
                            check_value(body, corrected_path(batch, case["id"], number, item))
                            for item in turn["checks"]
                        )
                        responses.append(body)
                else:
                    hard.append(
                        {
                            "id": "execution",
                            "status": "unknown" if started else "not_run",
                            "reason": "no complete HTTP response; never assume replay is safe",
                        }
                    )
                for row in hard:
                    automatic.append({**key, **row})
                    if row["status"] != "pass":
                        reasons.append(f"turn {number}: {row['id']}={row['status']}")
                turn_statuses = [r["status"] for r in hard]
                if (
                    case.get("revision") in {
                        "20261026-r2", "20261026-r4", "20261026-r5", "20261026-r6",
                    }
                    and any(c["id"] == "exposure" for c in turn["semantic"])
                    and (started or response_events)
                ):
                    try:
                        from .exposure import verify
                    except ImportError:
                        from exposure import verify
                    exposure = verify(batch, case["id"], attempt, number)
                    automatic.append({**key, **exposure})
                    turn_statuses.append(exposure["status"])
                    if exposure["status"] != "pass":
                        reasons.append(f"turn {number}: boundary_exposure=unknown")
                for criterion in turn["semantic"]:
                    template = {
                        **key,
                        "criterion": criterion["id"],
                        "expect": criterion["expect"],
                        "status": "unknown",
                        "reviewer": {"kind": "", "name": ""},
                        "reviewed_at": "",
                        "reason": "",
                        "evidence": [],
                        "version": VERSION,
                        "execution_sha256": fingerprint,
                    }
                    templates.append(template)
                    review = review_map.get((*key.values(), criterion["id"]), {})
                    status = (
                        review["status"]
                        if semantic_valid(review, batch, fingerprint)
                        else "unknown"
                    )
                    if started or response_events:
                        turn_statuses.append(status)
                        if status != "pass":
                            reasons.append(f"turn {number}: {criterion['id']}={status}")
                statuses.append(combined(turn_statuses))
                packets.append(
                    {
                        **key,
                        "turn": number,
                        "input": turn,
                        "oracle": case["oracle"],
                        "events": turn_events,
                    }
                )
            if responses:
                history = history_checks(matching, responses, case["turns"])
                automatic.extend({"case_id": case["id"], "attempt": attempt, **r} for r in history)
                statuses.extend(row["status"] for row in history)
                reasons.extend(f"history={r['status']}" for r in history if r["status"] != "pass")
            results.append(
                {
                    "case_id": case["id"],
                    "title": case["title"],
                    "suite": case["suite"],
                    "attempt": attempt,
                    "status": combined(statuses),
                    "reasons": reasons,
                }
            )
    # Archive analyses, not raw evidence. Existing semantic judgments are never overwritten.
    if checks_dir.exists() and any(checks_dir.glob("*.json*")):
        history = checks_dir / "history" / now().replace(":", "-")
        history.mkdir(parents=True)
        for path in [*checks_dir.glob("*.json*"), batch / "report.md"]:
            if path.is_file():
                shutil.copyfile(path, history / path.name)
    write_jsonl(checks_dir / "automatic.jsonl", automatic)
    write_jsonl(checks_dir / "semantic.template.jsonl", templates)
    if not (checks_dir / "semantic.jsonl").exists():
        write_jsonl(checks_dir / "semantic.jsonl", templates)
    write(checks_dir / "review_packets.json", packets)
    counts = dict(Counter(row["status"] for row in results))
    summary = {
        "version": VERSION,
        "counts": counts,
        "results": results,
        "excluded_case_ids": frozen["excluded_case_ids"],
        "execution_sha256": fingerprint,
        "annotation_corrections": (
            read(checks_dir / "path_corrections.json")
            if (checks_dir / "path_corrections.json").exists() else None
        ),
    }
    write(checks_dir / "summary.json", summary)
    labels = {"pass": "通过", "fail": "失败", "unknown": "无法判定", "not_run": "未执行"}
    lines = [
        "# 聊天 E2E 验收",
        "",
        "本批："
        + "，".join(f"{label} {counts.get(status, 0)}" for status, label in labels.items()),
        "",
        f"版本：{VERSION}；源码：{frozen['head']}；模型（操作者声明）：{frozen['target']['model']}。",
        "",
        "| 用例 | 次数 | 结论 | 缺口/失败项 |",
        "| --- | ---: | --- | --- |",
    ]
    for row in results:
        lines.append(
            f"| {row['case_id']} {row['title']} | {row['attempt']} | "
            f"{labels[row['status']]} | {'; '.join(row['reasons']) or '必要检查通过'} |"
        )
    lines.extend(
        [
            "",
            "范围与限制：仅实际聊天 API 与历史读回，不含浏览器、人工请求提交或支付退款。",
            "语义复检必须有独立判读者与证据；程序通过和 HTTP 200 不代表业务通过。",
            "服务版本/模型/数据为操作者声明，应核对服务配置或 Trace；不会自动读取服务器配置。",
            "未选用例：" + ", ".join(frozen["excluded_case_ids"]),
            "",
            (
                "细项见 checks/automatic.jsonl、semantic.jsonl、review_packets.json；"
                "原始调用见 evidence/executions.jsonl，冻结事实见 evidence/frozen.json。"
            ),
        ]
    )
    if summary["annotation_corrections"]:
        lines.append(
            "标注修订：checks/path_corrections.json；原冻结标注与旧检查均保留，"
            "仅修正契约字段路径，未修改预期值、未重新执行聊天。"
        )
    (batch / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()
    summary = render(args.batch)
    print(json.dumps(summary["counts"], ensure_ascii=False))
    raise SystemExit(0 if set(summary["counts"]) == {"pass"} else 1)
