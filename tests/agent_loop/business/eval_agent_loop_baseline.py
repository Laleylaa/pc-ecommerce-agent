"""P0 old-runtime baseline: real model, formal graph/tools, synthetic repositories/context.

No configured DB, embeddings, Tavily, LangSmith or chat persistence. All HTTP model retries
are disabled; wrappers count failed attempts and distinguish missing token usage.
"""

import argparse
import asyncio
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.core.llm import build_chat_model
from scripts.eval_goal_tool_flow import cases, evaluate, implementation_manifest

CASE_IDS = (
    "named_product", "mixed", "policy", "recent_orders", "second_candidate", "direct",
    "missing_object", "security", "ambiguous_reference", "facets_only", "knowledge",
    "alternative_budget",
)


class MeteredModel:
    def __init__(self, model, attempts=None, counts=None):
        self.model = model
        self.attempts = attempts if attempts is not None else []
        self.counts = counts if counts is not None else {}

    def bind_tools(self, definitions, **kwargs):
        return MeteredModel(
            self.model.bind_tools(definitions, **kwargs), self.attempts, self.counts
        )

    async def ainvoke(self, messages):
        # Tools' own planners use unbound calls; answer also uses an unbound model.
        from app.core.model_usage import model_call_counts
        counts = model_call_counts() or {}
        changed = [k for k, v in counts.items() if v > self.counts.get(k, 0)]
        self.counts.update(counts)
        item = {"seconds": None, "usage": None, "stage_counts": counts,
                "stage": changed[0] if len(changed) == 1 else "unknown"}
        self.attempts.append(item)
        started = time.monotonic()
        try:
            response = await self.model.ainvoke(messages)
            item["usage"] = response.usage_metadata or None
            return response
        except BaseException as exc:
            item["error_type"] = type(exc).__name__
            raise
        finally:
            item["seconds"] = round(time.monotonic() - started, 3)


def summarize(rows):
    durations = sorted(r["seconds"] for r in rows)
    attempts = [a for r in rows for a in r["attempts"]]
    usage = [a["usage"] for a in attempts if a.get("usage") is not None]
    return {
        "cases": len(rows),
        "engineering_successes": sum(r["engineering_passed"] for r in rows),
        "goal_coverage_check_passes": sum(r["goal_coverage_passed"] for r in rows),
        "model_attempts": len(attempts),
        "attempts_by_stage": {
            stage: sum(a["stage"] == stage for a in attempts)
            for stage in sorted({a["stage"] for a in attempts})
        },
        "known_input_tokens": sum(u["input_tokens"] for u in usage),
        "known_output_tokens": sum(u["output_tokens"] for u in usage),
        "usage_missing_attempts": len(attempts) - len(usage),
        "latency_p50_seconds": durations[math.ceil(len(durations) * .5) - 1],
        "latency_p95_seconds": durations[math.ceil(len(durations) * .95) - 1],
        "scoring": "Existing independent case expectations and tool-result checks; "
                   "goal coverage is structural. Answer faithfulness needs separate review.",
    }


async def main(args):
    from app.core.model_usage import collect_model_calls
    settings = get_settings().model_copy(update={"agent_trace_mode": "off"})
    if not settings.llm_api_key:
        raise SystemExit("No configured model; no calls made")
    selected = [c for c in cases() if c["id"] in (args.case or CASE_IDS)]
    if not selected:
        raise SystemExit("No cases matched")
    path = (Path(__file__).resolve().parents[3] / "backend/evals/results/agent_loop") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-baseline-" + uuid4().hex[:8] + ".json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "manifest": implementation_manifest(), "model": settings.llm_model,
        "provider": settings.llm_provider, "repeats": args.repeat,
        "data_mode": "formal old graph including answers; synthetic DB/context; no persistence",
        "results": [],
    }
    for repeat in range(args.repeat):
        for case in selected:
            meter = MeteredModel(
                build_chat_model(settings, temperature=0, max_retries=0, timeout=30)
            )
            started = time.monotonic()
            row = {"id": case["id"], "repeat": repeat, "attempts": meter.attempts}
            try:
                with collect_model_calls():
                    async with asyncio.timeout(240):
                        item = await evaluate(case, settings, model=meter, full_answer=True)
                row["result"] = item
                row["engineering_passed"] = not (
                    item["planning_failures"] or item["information_issues"]
                ) and bool(item["answer"])
                row["goal_coverage_passed"] = not item["planning_failures"] and bool(item["answer"])
            except Exception as exc:
                row.update(error_type=type(exc).__name__, engineering_passed=False,
                           goal_coverage_passed=False)
            row["seconds"] = round(time.monotonic() - started, 3)
            report["results"].append(row)
            report["summary"] = summarize(report["results"])
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(case["id"], row["engineering_passed"], row["seconds"],
                  len(meter.attempts), row.get("error_type", ""), flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Artifacts: {path}")
    return int(any(not r["engineering_passed"] for r in report["results"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 3:
        parser.error("--repeat must be between 1 and 3")
    raise SystemExit(asyncio.run(main(args)))
