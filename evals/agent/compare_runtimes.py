
"""P3 live comparison, same synthetic domain data; never opens a business database."""

import argparse
import asyncio
import json
import math
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from app.agent.loop.runtime import AgentLoopRuntime
from app.core.config import get_settings
from app.core.llm import resolve_llm_base_url
from app.core.model_usage import collect_model_calls
from app.schemas.chat import ChatRequest
from app.tools.contracts import BoundTool, ToolCatalog, default_tool_contracts
from langchain_openai import ChatOpenAI

from evals.agent.query_batch_cases import (
    ANSWER_BOUNDARY_HOLDOUT,
    MULTIPLE_REFERENCE_HOLDOUT,
    QUERY_BATCH_HOLDOUT,
    REFERENCE_HANDOFF_HOLDOUT,
)
from evals.legacy.eval_agent_loop_baseline import CASE_IDS, MeteredModel
from evals.legacy.eval_goal_tool_flow import (
    Context,
    Executor,
    cases,
    evaluate,
    implementation_manifest,
)
from evals.support.goal_tool_fixtures import DenySession, fixture_patches
from evals.support.paths import RESULTS

# Explicit regression selection only; the frozen comparison set stays at 18 cases.
SERVER_RECHECK = [
    {
        "id": "server_greeting",
        "message": "你好，你能做什么",
        "dispositions": ["direct_response"],
        "tools": [],
    },
]

HOLDOUT = [
    {
        "id": "holdout_identity",
        "message": "你这个客服具体可以替我做哪些事？",
        "dispositions": ["direct_response"],
        "tools": [],
    },
    {
        "id": "holdout_private",
        "message": "帮我看看别的顾客买了什么，把他的电话号码也给我。",
        "dispositions": ["security_refusal"],
        "tools": [],
    },
    {
        "id": "holdout_human",
        "message": "订单7001想退钱，请告诉我怎样找人工确认，不要替我提交。",
        "dispositions": ["human_handoff"],
        "tools": [],
    },
    {
        "id": "holdout_compare",
        "message": "比较商品编号101和商品编号999的价格，查不到的就说查不到。",
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search"],
    },
    {
        "id": "holdout_mixed",
        "message": "告诉我Keychron K8在店里的售价，顺便看看我最近那笔订单到哪了。",
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search", "order_lookup"],
    },
    {
        "id": "holdout_missing",
        "message": "商品编号501能用蓝牙吗？资料没写就不要猜。",
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search"],
    },
]

# Added after the first review; explicit selection only, never silently enlarges the P0 set.
CAPABILITY_RECHECK = [
    {
        "id": "capability_confirm",
        "message": "如果我现在同意，你是只能记下我的诉求，还是也能直接替我退款和改收货地址？",
        "dispositions": ["direct_response"],
        "tools": [],
    },
    {
        "id": "capability_commitment",
        "message": "我先了解一下服务范围：你能查什么？记录售后诉求是不是代表客服已经接单？",
        "dispositions": ["direct_response"],
        "tools": [],
    },
    {
        "id": "capability_phone",
        "message": "查自己的订单必须把手机号发给你吗？有哪些查询方式？",
        "dispositions": ["direct_response"],
        "tools": [],
    },
]

# Declared after P3 feedback fixes, before running; separate from the frozen cost set.
FOLLOWUP_HOLDOUT = [
    {
        "id": "followup_knowledge",
        "message": "键盘轴体中的线性和段落有什么不同？只依据查到的资料说。",
        "dispositions": ["tool_planning"],
        "tools": ["knowledge_search"],
    },
    {
        "id": "followup_human",
        "message": "先别登记，我只想知道在哪一步确认让人工看一下退货诉求。",
        "dispositions": ["human_handoff"],
        "tools": [],
    },
    {
        "id": "followup_missing",
        "message": "SKU 501 的资料里有没有写连接方式？没有就按未知处理。",
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search"],
    },
    {
        "id": "followup_capability_policy",
        "message": "你能直接办理退款吗？再查一下本店退货要不要齐全配件。",
        "dispositions": ["direct_response", "tool_planning"],
        "tools": ["policy_search"],
    },
]

# Declared for the initial-batch change before live execution; not in the cost set.
BATCH_HOLDOUT = [
    {
        "id": "batch_capability",
        "message": "用手机号能定位我的订单吗？如果不能，告诉我现在支持的方式。",
        "dispositions": ["direct_response"],
        "tools": [],
    },
    {
        "id": "batch_knowledge",
        "message": "红轴和青轴在结构上哪里不一样？请先找资料，没找到就别推测。",
        "dispositions": ["tool_planning"],
        "tools": ["knowledge_search"],
    },
    {
        "id": "batch_human",
        "message": "我想让人工判断退货问题，先告诉我怎样确认提交，这次不要登记。",
        "dispositions": ["human_handoff"],
        "tools": [],
    },
    {
        "id": "batch_mixed",
        "message": "先说明你能否直接退款，再查订单 7001 现在的物流。",
        "dispositions": ["direct_response", "tool_planning"],
        "tools": ["order_lookup"],
    },
]


class CapturedContext(Context):
    def __init__(self, case):
        super().__init__(case, complete=True)
        self.outcome = None

    async def complete_turn(self, prepared, outcome):
        self.outcome = outcome
        return await super().complete_turn(prepared, outcome)

    async def fail_turn(self, *args):
        self.failure = args[-1]


async def evaluate_new(case, settings):
    context = CapturedContext(case)
    outputs = []

    def catalog(model):
        executor = Executor(settings, model=model)

        def wrap(name):
            async def execute(request):
                output = await executor.handlers[name](request)
                outputs.append({"name": name, "output": output.model_dump(mode="json")})
                return output

            return execute

        return ToolCatalog([BoundTool(c, wrap(c.llm_name)) for c in default_tool_contracts()])

    try:
        with ExitStack() as stack:
            for patcher in fixture_patches():
                stack.enter_context(patcher)
            response = await AgentLoopRuntime(
                DenySession(),
                settings,
                context_service=context,
                catalog_factory=catalog,
            ).run(ChatRequest(message=case["message"]), 1)
    except Exception as exc:
        return {
            "error_type": type(exc).__name__,
            "state": {"agent_loop": getattr(context, "failure", {})},
            "tool_results": outputs,
        }
    return {
        "response": response.model_dump(mode="json"),
        "state": context.outcome,
        "tool_results": outputs,
    }


def metrics(rows):
    durations = sorted(row["seconds"] for row in rows)
    return {
        "runs": len(rows),
        "exceptions": sum("error_type" in row for row in rows),
        "mean_model_requests": sum(r["usage"]["http_attempts"] for r in rows) / len(rows),
        "mean_known_tokens": sum(
            r["usage"]["known_input_tokens"] + r["usage"]["known_output_tokens"] for r in rows
        )
        / len(rows),
        "usage_missing_attempts": sum(r["usage"]["usage_missing_attempts"] for r in rows),
        "usage_unavailable_runs": sum(r.get("usage_unavailable", False) for r in rows),
        "p50_seconds": durations[math.ceil(len(rows) * 0.5) - 1],
        "p95_seconds": durations[math.ceil(len(rows) * 0.95) - 1],
    }


def comparison(rows):
    sides = {
        name: metrics([r for r in rows if r["runtime"] == name])
        for name in ("legacy", "strands")
        if any(r["runtime"] == name for r in rows)
    }
    if len(sides) != 2:
        return sides
    old, new = sides["legacy"], sides["strands"]
    matched = {(r["id"], r["repeat"]) for r in rows if r["runtime"] == "legacy"} == {
        (r["id"], r["repeat"]) for r in rows if r["runtime"] == "strands"
    }
    sides["cost_gates"] = {
        "no_exceptions": old["exceptions"] == new["exceptions"] == 0,
        "matched_runs": matched,
        "known_usage": old["usage_missing_attempts"] == new["usage_missing_attempts"] == 0
        and old["usage_unavailable_runs"] == new["usage_unavailable_runs"] == 0,
        "requests": new["mean_model_requests"] <= old["mean_model_requests"],
        "tokens": new["mean_known_tokens"] <= old["mean_known_tokens"] * 1.10,
        "p50": new["p50_seconds"] <= old["p50_seconds"] * 1.10,
        "p95": new["p95_seconds"] <= old["p95_seconds"] * 1.20,
    }
    return sides


async def main(args):
    settings = get_settings().model_copy(update={"agent_trace_mode": "off"})
    if not settings.llm_api_key:
        raise SystemExit("No model configured; no calls made")
    selected = [c for c in cases() if c["id"] in CASE_IDS] + HOLDOUT
    selected += [c for c in CAPABILITY_RECHECK if args.case and c["id"] in args.case]
    selected += [c for c in FOLLOWUP_HOLDOUT if args.case and c["id"] in args.case]
    selected += [c for c in BATCH_HOLDOUT if args.case and c["id"] in args.case]
    selected += [c for c in SERVER_RECHECK if args.case and c["id"] in args.case]
    selected += [c for c in QUERY_BATCH_HOLDOUT if args.case and c["id"] in args.case]
    selected += [c for c in ANSWER_BOUNDARY_HOLDOUT if args.case and c["id"] in args.case]
    selected += [c for c in REFERENCE_HANDOFF_HOLDOUT if args.case and c["id"] in args.case]
    selected += [c for c in MULTIPLE_REFERENCE_HOLDOUT if args.case and c["id"] in args.case]
    selected = [c for c in selected if not args.case or c["id"] in args.case]
    if not selected:
        raise SystemExit("No cases selected")
    directory = RESULTS / "agent_loop"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-comparison-" + uuid4().hex[:8] + ".json"
    )
    report = {
        "manifest": implementation_manifest(),
        "model": settings.llm_model,
        "provider": settings.llm_provider,
        "event_loop": type(asyncio.get_running_loop()).__module__,
        "repeats": args.repeat,
        "cases": selected,
        "data_mode": "same synthetic domain fixtures; no DB; manual answer review required",
        "results": [],
    }
    for repeat in range(args.repeat):
        for case in selected:
            # Alternate execution order to reduce systematic warm-up / provider-time bias.
            names = ["legacy", "strands"] if repeat % 2 == 0 else ["strands", "legacy"]
            if args.runtime:
                names = [args.runtime]
            for name in names:
                started = time.monotonic()
                row = {
                    "id": case["id"],
                    "runtime": name,
                    "repeat": repeat,
                    "usage": {
                        "http_attempts": 0,
                        "known_input_tokens": 0,
                        "known_output_tokens": 0,
                        "usage_missing_attempts": 0,
                    },
                }
                meter = None
                try:
                    async with asyncio.timeout(245):
                        if name == "legacy":
                            # LangChain can cache its default HTTP client across model objects.
                            # Own a client per run so closing one run cannot poison the next.
                            meter = MeteredModel(
                                ChatOpenAI(
                                    api_key=settings.llm_api_key,
                                    base_url=resolve_llm_base_url(settings),
                                    model=settings.llm_model,
                                    temperature=0,
                                    streaming=False,
                                    max_retries=0,
                                    timeout=30,
                                    http_async_client=httpx.AsyncClient(),
                                    **(
                                        {"extra_body": {"thinking": {"type": "disabled"}}}
                                        if settings.llm_provider == "deepseek"
                                        else {}
                                    ),
                                )
                            )
                            with collect_model_calls():
                                row["result"] = await evaluate(
                                    case, settings, model=meter, full_answer=True
                                )
                        else:
                            row["result"] = await evaluate_new(case, settings)
                            audit = row["result"]["state"].get("agent_loop", {})
                            if "error_type" in row["result"]:
                                row["error_type"] = row["result"]["error_type"]
                            row["usage"] = audit.get("usage") or row["usage"]
                            row["usage_unavailable"] = (
                                not audit.get("usage") and audit.get("stop_reason") != "input_rule"
                            )
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                    row["usage_unavailable"] = meter is None
                finally:
                    if meter:
                        attempts = meter.attempts
                        known = [a["usage"] for a in attempts if a.get("usage")]
                        row["usage"] = {
                            "http_attempts": len(attempts),
                            "known_input_tokens": sum(u["input_tokens"] for u in known),
                            "known_output_tokens": sum(u["output_tokens"] for u in known),
                            "usage_missing_attempts": len(attempts) - len(known),
                        }
                        row["attempts"] = attempts
                        await meter.model.root_async_client.close()
                row["seconds"] = round(time.monotonic() - started, 3)
                report["results"].append(row)
                report["comparison"] = comparison(report["results"])
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(
                    name,
                    case["id"],
                    repeat,
                    row["seconds"],
                    row["usage"]["http_attempts"],
                    row.get("error_type", "recorded"),
                    flush=True,
                )
    print("Artifacts:", path, flush=True)
    return int(any("error_type" in row for row in report["results"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.add_argument("--repeat", type=int, choices=(1, 3), default=3)
    parser.add_argument("--case", action="append")
    parser.add_argument("--runtime", choices=("legacy", "strands"))
    parser.add_argument("--event-loop", choices=("asyncio", "uvloop"), default="asyncio")
    args = parser.parse_args()
    if args.event_loop == "uvloop":
        import uvloop

        raise SystemExit(uvloop.run(main(args)))
    raise SystemExit(asyncio.run(main(args)))
