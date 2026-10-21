"""Run customer-service eval CSVs against the chat API.

Examples:
    python -m evals.agent.run_customer_service_evals --suite 01_can_do --limit-per-file 2
    python -m evals.agent.run_customer_service_evals --suite 01_can_do/tooluse
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from evals.support.paths import project_path

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api"
DEFAULT_EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals/agent/customer_service_requests_v2"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "artifacts/evals"
DEMO_CREDENTIALS = ("demo@example.com", "demo-password")
ISOLATION_CREDENTIALS = ("test_user_001@example.com", "isolation-demo-password")

RESULT_FIELDS = [
    "case_id",
    "source_file",
    "level1",
    "level2",
    "level3",
    "user_query",
    "conversation_context",
    "expected_disposition",
    "expected_tools",
    "actual_disposition",
    "actual_tools",
    "actual_answer",
    "raw_response_json",
    "pass_fail",
    "failure_type",
    "notes",
    "elapsed_ms",
]

TOOL_NAME_MAPPING = {
    "catalog_search": "catalog.search",
    "catalog_facets": "catalog.facets",
    "order_lookup": "order.lookup",
    "policy_search": "policy.search",
    "knowledge_search": "knowledge.search",
}

FORBIDDEN_EXECUTION_PHRASES = ["已取消", "已退款", "已提交", "已修改", "已下单"]


@dataclass(frozen=True)
class EvalCase:
    row: dict[str, str]
    source_file: Path
    eval_root: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="01_can_do", help="Subdirectory under eval root.")
    parser.add_argument("--eval-root", type=project_path, default=DEFAULT_EVAL_ROOT)
    parser.add_argument("--output-dir", type=project_path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--limit-per-file", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args()


def load_cases(eval_root: Path, suite: str, limit_per_file: int | None) -> list[EvalCase]:
    suite_path = eval_root / suite
    if not suite_path.exists():
        raise SystemExit(f"suite path does not exist: {suite_path}")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    duplicates: list[str] = []

    for csv_path in sorted(suite_path.rglob("*.csv")):
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        if limit_per_file is not None:
            rows = rows[:limit_per_file]

        for row in rows:
            case_id = row.get("case_id", "")
            if case_id in seen:
                duplicates.append(case_id)
            seen.add(case_id)
            cases.append(EvalCase(row=row, source_file=csv_path, eval_root=eval_root))

    if duplicates:
        raise SystemExit(f"duplicate case_id: {duplicates[:10]}")
    return cases


def credentials_for_context(context: str) -> tuple[str, str]:
    if "current_user_id=2" in context:
        return ISOLATION_CREDENTIALS
    return DEMO_CREDENTIALS


def prelude_messages_for_case(case: EvalCase) -> list[str]:
    """Create real previous turns for context-memory eval cases.

    The CSV context field documents the desired memory state, but the HTTP API only
    accepts normal chat turns. These prelude messages build that state through the
    same public API path as a user conversation.
    """
    source_name = case.source_file.name
    context = case.row.get("conversation_context", "")
    if case.row.get("level2") != "context_memory" and not context.startswith("memory."):
        return []

    if source_name == "follow_up_order_reference.csv":
        return ["帮我查一下我最近的订单。"]
    if source_name == "follow_up_product_reference.csv":
        return ["帮我推荐一款 500 元以内的无线鼠标，最好有白色版本。"]
    if source_name == "preference_carryover.csv":
        return ["我想买一个办公用的无线键盘，预算 500 以内，最好安静一点。"]
    if source_name == "selected_sku_or_spu_artifact.csv":
        if "selected_spu_ids" in context:
            return ["比较一下 SPU 1 和 SPU 2 两个系列。"]
        return ["帮我推荐两款无线鼠标，给出价格、库存和主要规格。"]
    if source_name == "ambiguous_reference_with_context.csv":
        return ["帮我推荐两款无线鼠标，并说明第一款如果不合适能不能退。"]
    return []


async def login(
    client: httpx.AsyncClient,
    base_url: str,
    credentials: tuple[str, str],
) -> str:
    response = await client.post(
        f"{base_url}/auth/login",
        json={"login_identifier": credentials[0], "password": credentials[1]},
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def normalize_tool_name(name: str) -> str:
    if name in TOOL_NAME_MAPPING:
        return TOOL_NAME_MAPPING[name]
    return name


def normalize_actual_disposition(
    intent: Any,
    boundary: Any,
    actual_tools: list[str],
) -> str:
    if boundary == "human_handoff_required":
        return "human_handoff"
    if boundary in {"out_of_scope", "unsupported", "security_refusal"}:
        return str(boundary)
    if actual_tools:
        return "tool_planning"
    if intent in {"general", "direct_response"}:
        return "direct_response"
    return str(intent or boundary or "unknown")


def expected_tool_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(";") if item.strip()}


def actual_tool_set(values: list[str]) -> set[str]:
    return {normalize_tool_name(value) for value in values}


def grade(
    row: dict[str, str],
    actual_disposition: str,
    tools: list[str],
    response: dict[str, Any],
) -> tuple[str, str, str]:
    expected_disposition = row.get("expected_disposition", "")
    expected_tools = expected_tool_set(row.get("expected_tools", ""))
    actual_tools = actual_tool_set(tools)
    problems: list[str] = []

    if expected_disposition and expected_disposition != actual_disposition:
        problems.append("router_wrong_disposition")
    if expected_tools and not expected_tools.issubset(actual_tools):
        problems.append("wrong_tool")
    if not expected_tools and actual_tools:
        problems.append("unexpected_tool")

    answer = str(response.get("answer") or "")
    forbidden = row.get("expected_forbidden_behavior", "")
    if forbidden and "不得声称已" in forbidden:
        if any(word in answer for word in FORBIDDEN_EXECUTION_PHRASES):
            problems.append("answer_forbidden_behavior")

    if problems:
        return "fail", ";".join(problems), "script checks failed; inspect raw_response_json"
    if row.get("expected_key_assertions"):
        return (
            "manual_review",
            "",
            "automatic checks passed; verify expected_key_assertions manually",
        )
    return "pass", "", "automatic checks passed"


async def run_case(
    client: httpx.AsyncClient,
    base_url: str,
    case: EvalCase,
    tokens: dict[tuple[str, str], str],
    timeout: float,
) -> dict[str, str]:
    row = case.row
    credentials = credentials_for_context(row.get("conversation_context", ""))
    if credentials not in tokens:
        tokens[credentials] = await login(client, base_url, credentials)

    headers = {"Authorization": f"Bearer {tokens[credentials]}"}
    started = time.perf_counter()
    raw: dict[str, Any] = {}
    tools: list[str] = []
    actual_disposition = "error"
    answer = ""

    try:
        conversation_id: int | None = None
        prelude_summaries: list[dict[str, Any]] = []
        for prelude in prelude_messages_for_case(case):
            payload: dict[str, Any] = {"message": prelude}
            if conversation_id is not None:
                payload["conversation_id"] = conversation_id
            prelude_response = await client.post(
                f"{base_url}/chat",
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            prelude_response.raise_for_status()
            prelude_json = prelude_response.json()
            conversation_id = int(prelude_json["conversation_id"])
            prelude_summaries.append(
                {
                    "message": prelude,
                    "conversation_id": conversation_id,
                    "intent": prelude_json.get("intent"),
                    "answer_preview": str(prelude_json.get("answer") or "")[:240],
                }
            )

        request_payload: dict[str, Any] = {"message": row["user_query"]}
        if conversation_id is not None:
            request_payload["conversation_id"] = conversation_id

        response = await client.post(
            f"{base_url}/chat",
            json=request_payload,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        raw = response.json()
        if prelude_summaries:
            raw = {"prelude_messages": prelude_summaries, "response": raw}
        answer = str((raw.get("response") or raw).get("answer") or "")
        final_response = raw.get("response") or raw
        details = final_response.get("execution_details")
        if not isinstance(details, dict) or not isinstance(details.get("tool_calls"), list):
            raise ValueError("Chat response lacks execution_details.tool_calls for grading")
        tools = [normalize_tool_name(call["tool_name"]) for call in details["tool_calls"]]
        boundary = final_response.get("boundary") or {}
        boundary_classification = None
        if isinstance(boundary, dict):
            boundary_classification = boundary.get("classification")
        actual_disposition = normalize_actual_disposition(
            final_response.get("intent"),
            boundary_classification,
            tools,
        )
        grade_response = final_response

        pass_fail, failure_type, notes = grade(
            row,
            actual_disposition,
            tools,
            grade_response,
        )
    except Exception as exc:  # noqa: BLE001 - eval runner should record failures.
        raw = {"error": type(exc).__name__, "message": str(exc)}
        pass_fail, failure_type, notes = "error", "request_error", str(exc)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "case_id": row.get("case_id", ""),
        "source_file": str(case.source_file.relative_to(case.eval_root)),
        "level1": row.get("level1", ""),
        "level2": row.get("level2", ""),
        "level3": row.get("level3", ""),
        "user_query": row.get("user_query", ""),
        "conversation_context": row.get("conversation_context", ""),
        "expected_disposition": row.get("expected_disposition", ""),
        "expected_tools": row.get("expected_tools", ""),
        "actual_disposition": actual_disposition,
        "actual_tools": ";".join(sorted(actual_tool_set(tools))),
        "actual_answer": answer,
        "raw_response_json": json.dumps(raw, ensure_ascii=False, default=str),
        "pass_fail": pass_fail,
        "failure_type": failure_type,
        "notes": notes,
        "elapsed_ms": str(elapsed_ms),
    }


async def main() -> None:
    args = parse_args()
    cases = load_cases(args.eval_root, args.suite, args.limit_per_file)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_suite = args.suite.strip("/").replace("/", "_") or "all"
    csv_path = args.output_dir / f"{safe_suite}_{stamp}.csv"
    jsonl_path = args.output_dir / f"{safe_suite}_{stamp}.jsonl"
    summary_path = args.output_dir / f"{safe_suite}_{stamp}_summary.json"

    tokens: dict[tuple[str, str], str] = {}
    counts: Counter[str] = Counter()

    async with httpx.AsyncClient() as client:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
            with jsonl_path.open("w", encoding="utf-8") as jsonl_file:
                writer = csv.DictWriter(csv_file, fieldnames=RESULT_FIELDS)
                writer.writeheader()
                for index, case in enumerate(cases, start=1):
                    result = await run_case(
                        client,
                        args.base_url.rstrip("/"),
                        case,
                        tokens,
                        args.timeout,
                    )
                    writer.writerow(result)
                    csv_file.flush()
                    jsonl_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                    jsonl_file.flush()
                    counts[result["pass_fail"]] += 1
                    print(
                        f"[{index}/{len(cases)}] {result['pass_fail']} "
                        f"{result['case_id']} {result['failure_type']}"
                    )

    summary = {
        "suite": args.suite,
        "case_count": len(cases),
        "result_count": sum(counts.values()),
        "counts": dict(counts),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
