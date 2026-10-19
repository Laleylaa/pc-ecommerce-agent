"""TI-01/TI-03 live acceptance with synthetic data and no database/persistence.

Run from backend: .venv/bin/python -m scripts.eval_contract_acceptance --live
The fixed expectations are independent of model output. Review answers in the JSON too.
"""

import argparse
import asyncio
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.graph import AgentRuntime
from app.core.config import get_settings
from app.core.llm import build_chat_model
from app.schemas.chat import ChatRequest
from app.services.context import _next_working_memory
from scripts.eval_goal_planning import RecordingModel, assess, evaluate
from scripts.eval_goal_tool_flow import Audit, Context, Executor
from scripts.goal_tool_fixtures import DenySession, fixture_patches

OUTPUT = Path("evals/goal_planning/contract-acceptance-results.json")


def case(identity, message, tools=(), disposition="tool_planning", **extra):
    return dict(
        id=identity, message=message, tools=list(tools), dispositions=[disposition], **extra
    )


CASES = [
    case(
        "explicit_order",
        "帮我看一下订单号7001的包裹现在到哪了",
        ["order_lookup"],
        order_id=7001,
        full=True,
    ),
    case("short_order", "订单号123，麻烦查一下进展", ["order_lookup"], order_id=123),
    case(
        "historical_order",
        "刚才那笔订单的物流有更新吗",
        ["order_lookup"],
        memory={"order": {"last_order_id": 7001, "candidate_order_ids": [7001]}},
        history=[{"role": "assistant", "content": "刚查到你的订单7001已经发货。"}],
        bound_order=True,
    ),
    case("layout_choices", "店里的键盘在配列上都有哪些选择？", ["catalog_facets"]),
    case("object_layout", "SKU 201这款键盘具体是什么配列？", ["catalog_search"]),
    case("products_two", "挑两款白色的蓝牙键盘给我看看", ["catalog_search"], limit=2, full=True),
    case("products_default", "找找白色的蓝牙键盘", ["catalog_search"], limit=None, full=True),
    case(
        "products_fewer",
        "给我看两款粉色键盘",
        ["catalog_search"],
        limit=2,
        full=True,
        product_count=1,
    ),
    case("product_absent", "查一下SKU 999的价格", ["catalog_search"], full=True, product_count=0),
    case(
        "catalog_failure",
        "给我找两款白色键盘",
        ["catalog_search"],
        limit=2,
        full=True,
        inject_failure=True,
    ),
    dict(
        id="mixed_page_request",
        message="先给我看两款白色蓝牙键盘，另外我能跳到第三页吗？",
        tools=["catalog_search"],
        dispositions=["tool_planning", "direct_response"],
        limit=2,
        full=True,
        product_count=2,
    ),
    case("orders_two", "列一下我最近的两笔订单", ["order_lookup"], limit=2),
    case("orders_default", "我最近有哪些订单？", ["order_lookup"], limit=None),
    case(
        "knowledge_one",
        "找一条关于红轴和青轴差别的知识资料",
        ["knowledge_search"],
        limit=1,
        full=True,
    ),
    case("policy_one", "检索一条键盘拆封退货的店铺政策给我", ["policy_search"], limit=1),
    case("policy_bullets", "键盘拆封还能退吗？按三点给我讲明白", ["policy_search"], limit=None),
    case(
        "reuse_history",
        "把刚才的建议压缩成一句话就好",
        [],
        disposition="session_grounded_response",
        full=True,
        history=[{"role": "assistant", "content": "买键盘先确定预算，再看连接方式和配列。"}],
    ),
    case(
        "missing_history",
        "把你刚才说的那段建议再讲一遍",
        [],
        disposition="clarification",
        full=True,
    ),
    case("explicit_page", "给我跳到白色键盘的第3页", [], disposition="direct_response", full=True),
    case(
        "exclude_previous",
        "接着上一批往后翻，已经展示过的键盘不要再给我",
        [],
        disposition="direct_response",
        full=True,
    ),
    case(
        "tool_failure",
        "请查询订单7001的物流进度",
        ["order_lookup"],
        order_id=7001,
        full=True,
        inject_failure=True,
    ),
    case(
        "explicit_knowledge_retrieval",
        "请检索知识库，返回一条关于红轴和青轴差别的资料，并注明资料来源，不要仅凭常识回答",
        ["knowledge_search"],
        limit=1,
        full=True,
    ),
    dict(
        id="mixed_history_live",
        message="把刚才的建议压缩成一句话，再查一下订单7001现在的物流",
        tools=["order_lookup"],
        dispositions=["session_grounded_response", "tool_planning"],
        history=[{"role": "assistant", "content": "买键盘先确定预算，再看连接方式和配列。"}],
        order_id=7001,
        full=True,
    ),
]


class CompleteContext(Context):
    async def complete_turn(self, prepared, state):
        return SimpleNamespace(
            working_memory=_next_working_memory(prepared.working_memory, state),
            memory_changes=[],
        )


class FullRecordingModel(RecordingModel):
    async def ainvoke(self, messages):
        return await self.model.ainvoke(messages)


class FailingExecutor(Executor):
    async def execute(self, contract, arguments, runtime_context):
        async def unavailable(_request):
            raise OSError("synthetic repository unavailable")

        self.handlers[contract.name] = unavailable
        return await super().execute(contract, arguments, runtime_context)


async def evaluate_chat(item, settings):
    recorder = FullRecordingModel(build_chat_model(settings, temperature=0, max_retries=0))
    executor = (FailingExecutor if item.get("inject_failure") else Executor)(settings)
    runtime = AgentRuntime(
        DenySession(),
        settings,
        chat_model=recorder,
        router_model=recorder,
        tool_executor=executor,
        context_service=CompleteContext(item),
    )
    state = runtime._initial_state(ChatRequest(message=item["message"]), 1)
    visited = []
    with ExitStack() as stack:
        for replacement in fixture_patches():
            stack.enter_context(replacement)
        stack.enter_context(patch("app.agent.graph.ConversationRepository", Audit))
        async for update in runtime._build_graph().astream(state, stream_mode="updates"):
            for name, values in update.items():
                visited.append(name)
                state.update(values)
    response = runtime._response_from_state(state).model_dump(mode="json")
    failures = assess(item, state)
    if not response["answer"].strip() or "persist_turn" not in visited:
        failures.append("Chat response did not complete")
    results = state.get("tool_results", [])
    if item["tools"] and not results:
        failures.append("No business tool result")
    if item.get("inject_failure"):
        if len(executor.calls) != 1 or not results or results[0]["execution"]["ok"]:
            failures.append("Injected I/O failure did not stop after one tool call")
        if response["order"] or response["orders"]:
            failures.append("Failure fabricated order cards")
        if response["products"]:
            failures.append("Failure fabricated product cards")
    else:
        for result in results:
            if not result["execution"]["ok"]:
                failures.append(f"Tool failed: {result['name']}")
        if item["id"] == "products_two" and len(response["products"]) != 2:
            failures.append("Expected two displayed product cards")
        if item["id"] == "explicit_order" and not response["order"]:
            failures.append("Expected an order card")
        if "product_count" in item and len(response["products"]) != item["product_count"]:
            failures.append(f"Expected {item['product_count']} displayed products")
    return dict(
        id=item["id"],
        input=item,
        failures=failures,
        llm1=state.get("goal_plan"),
        llm2=state.get("execution_plan"),
        visited=visited,
        response=response,
        llm1_calls=state.get("request_router_call_count", 0),
        llm2_calls=state.get("task_planner_call_count", 0),
        attempt_outputs=recorder.outputs,
        transport_errors=recorder.errors,
        tool_calls=executor.calls,
        tool_results=results,
        tool_planner_outputs=executor.tool_model.outputs,
    )


def check_arguments(item, result):
    tasks = (result.get("llm2") or {}).get("tasks", [])
    for task in tasks:
        args = task["arguments"]
        if "limit" in item:
            if item["limit"] is None and "limit" in args:
                result["failures"].append("Unspecified limit was not omitted")
            elif item["limit"] is not None and args.get("limit") != item["limit"]:
                result["failures"].append(f"Expected explicit limit {item['limit']}")
        if "order_id" in item and args.get("order_id") != item["order_id"]:
            result["failures"].append("Explicit order_id not passed structurally")
        if item.get("bound_order") and ("order_id" in args or not task.get("bindings")):
            result["failures"].append("Historical identity must use a trusted binding")
        if "offset" in args or "page_from" in task:
            result["failures"].append("Obsolete pagination reached planning output")
    for call in result.get("tool_calls", []):
        if "limit" in item and call["arguments"].get("limit") != item["limit"]:
            result["failures"].append("Runtime changed the planned limit")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", action="append")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required to enable model calls")
    settings = get_settings()
    if not settings.llm_api_key:
        raise SystemExit("No configured model; no calls made")
    selected = [c for c in CASES if not args.case or c["id"] in args.case]
    if not selected:
        parser.error("No selected cases")
    sources = [
        *Path("app").rglob("*.py"),
        Path(__file__),
        Path("scripts/eval_goal_planning.py"),
        Path("scripts/eval_goal_tool_flow.py"),
        Path("scripts/goal_tool_fixtures.py"),
    ]
    report = dict(
        model=settings.llm_model,
        mode="live models; synthetic repositories; no configured DB, HTTP or browser",
        source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        results=[],
    )
    for item in selected:
        try:
            evaluator = evaluate_chat if item.get("full") else evaluate
            result = await asyncio.wait_for(evaluator(item, settings), timeout=150)
            check_arguments(item, result)
        except Exception as exc:
            # Do not serialize exception messages: provider URLs may contain credentials.
            result = dict(id=item["id"], input=item, failures=[type(exc).__name__])
        report["results"].append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        print(item["id"], result["failures"] or "PASS", flush=True)
    return int(any(r["failures"] for r in report["results"]))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
