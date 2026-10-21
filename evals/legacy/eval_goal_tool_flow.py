"""Formal graph/tools over synthetic repositories; CLI stops before answering.

P0 baseline may opt into full answering with in-memory completion via evaluate().
"""

import argparse
import asyncio
import json
from contextlib import ExitStack
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from subprocess import run
from unittest.mock import patch
from uuid import uuid4

from app.agent.legacy.answer_context import build_answer_context
from app.agent.legacy.runtime import AgentRuntime
from app.core.config import Settings, get_settings
from app.core.llm import build_chat_model
from app.core.tracing import agent_trace, trace_boundary, trace_data
from app.schemas.chat import ChatRequest
from app.schemas.context import MemoryChanges, PreparedTurn, WorkingMemoryV2
from app.services.context import _next_working_memory
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.contracts import BoundTool, execute_bound_tool
from app.tools.orders import LLMOrderQueryPlanner, OrderToolService
from langchain_core.messages import AIMessage

from evals.legacy.eval_goal_planning import CASE_PATH, RecordingModel, assess
from evals.support.goal_tool_fixtures import (
    DenySession,
    SearchRecords,
    fixture_patches,
    knowledge_service,
    web_search_service,
)
from evals.support.paths import project_path


class Context:
    def __init__(self, case, *, complete=False):
        self.case = case
        self.complete = complete

    async def prepare_turn(self, user_id, conversation_id, text):
        return PreparedTurn(
            user_id=user_id,
            conversation_id=1,
            user_message_id=1,
            run_id=1,
            message=text,
            history=self.case.get("history", []),
            memory=[],
            working_memory=WorkingMemoryV2.model_validate(self.case.get("memory", {})),
        )

    async def complete_turn(self, prepared, outcome):
        if self.complete:
            return MemoryChanges(
                working_memory=_next_working_memory(prepared.working_memory, outcome)
            )
        raise AssertionError("Must stop before LLM3 and persistence")


class Audit:
    def __init__(self, session):
        assert isinstance(session, DenySession)

    async def add_tool_call(self, *args):
        pass


class ToolModel:
    def __init__(self, model):
        self.model = model
        self.outputs = []

    async def ainvoke(self, messages):
        response = await self.model.ainvoke(messages)
        self.outputs.append({"content": response.content, "tool_calls": response.tool_calls})
        return response


class Executor:
    def __init__(self, settings, model=None):
        self.calls = []
        self.tool_model = ToolModel(
            model if model is not None else build_chat_model(settings, temperature=0, max_retries=0)
        )
        search = CatalogSearchService(SearchRecords(), CatalogSearchPlanner(self.tool_model))
        catalog = CatalogToolService(
            DenySession(), planner=LLMCatalogQueryPlanner(self.tool_model)
        )
        order = OrderToolService(DenySession(), planner=LLMOrderQueryPlanner(self.tool_model))
        knowledge = knowledge_service()
        self.handlers = {
            "catalog_search": search.search,
            "catalog_facets": catalog.facets,
            "order_lookup": order.lookup,
            "policy_search": knowledge.search_policy,
            "knowledge_search": web_search_service().search,
        }

    async def execute(self, contract, arguments, runtime_context):
        self.calls.append(
            {"name": contract.name, "arguments": arguments, "runtime_context": runtime_context}
        )
        return await execute_bound_tool(
            BoundTool(contract, self.handlers[contract.name]), arguments, runtime_context
        )


def inspect_results(case, state):
    issues = []
    results = state.get("tool_results", [])
    if state.get("execution_plan") and not results:
        issues.append("No tool result despite execution plan")
    for result in results:
        execution = result["execution"]
        if not execution["ok"]:
            issues.append(
                f"{result['name']}: execution failed ({execution.get('error', {}).get('code')})"
            )
            continue
        output = execution["output"]
        if result["name"] == "catalog_search":
            if output.get("query_state") != "completed":
                issues.append(
                    f"Search not executed: {output.get('status')}/{output.get('reason_code')}"
                )
            products = output.get("products", [])
            ids = {sku for p in products for sku in p.get("candidate_sku_ids", [])}
            if case["id"] in {
                "budget_update",
                "office_layout",
                "office_paraphrase",
                "office_without_facets",
            }:
                if products and (ids != {101, 201, 301} or output.get("total_match_count") != 3):
                    issues.append(
                        f"Expected white/Bluetooth/<=300 exact SKUs 101,201,301; got {sorted(ids)}"
                    )
            if case["id"].startswith("office") and products:
                issues.append(
                    "Review needed: products do not prove office suitability; "
                    "inspect unresolved-scene evidence"
                )
            if case["id"] == "alternative_budget" and output.get("match_kind") != "alternative":
                issues.append("Expected alternatives, not strict matches")
            if case["id"] == "missing_fact" and (
                output.get("match_kind") != "object_facts" or not output.get("unknown_sku_count")
            ):
                issues.append("Expected object_facts and unknown connection fact")
            if case["id"] == "second_candidate" and ids != {201}:
                issues.append(f"Second-candidate binding expected SKU201; got {sorted(ids)}")
            if case["id"] == "missing_object" and output.get("status") != "not_found":
                issues.append("Expected not_found for absent SKU")
        elif result["name"] == "catalog_facets":
            if output.get("facet") != "spec_value" or output.get("spec_key") != "style":
                issues.append("Layout request did not return style values")
            if not output.get("items"):
                issues.append("No facet choices")
        elif result["name"] == "order_lookup":
            if output.get("returned_count") != 1:
                issues.append("Expected one synthetic order")
            if "测试包裹运输中" not in json.dumps(output, ensure_ascii=False):
                issues.append("Order result lost logistics event")
        elif result["name"] in {"policy_search", "knowledge_search"}:
            if not output.get("documents"):
                issues.append("No retrieved evidence")
    for status in state.get("task_status", {}).values():
        if status["status"] in {"failed", "blocked", "pending"}:
            issues.append(f"{status['task_id']}: {status['status']} ({status.get('reason')})")
    return issues


async def evaluate(case, settings, *, model=None, full_answer=False):
    request = ChatRequest(message=case["message"])
    recorder = RecordingModel(
        model if model is not None else build_chat_model(settings, temperature=0, max_retries=0)
    )
    executor = Executor(settings, model=model)
    runtime = AgentRuntime(
        DenySession(),
        settings,
        chat_model=recorder,
        router_model=recorder,
        tool_executor=executor,
        context_service=Context(case, complete=full_answer),
    )
    state = runtime._initial_state(request, 1)
    if full_answer:
        state["turn_deadline_monotonic"] = asyncio.get_running_loop().time() + 240
    visited = []
    with ExitStack() as stack:
        for replacement in fixture_patches():
            stack.enter_context(replacement)
        stack.enter_context(patch("app.agent.legacy.runtime.ConversationRepository", Audit))
        stream = runtime._build_graph().astream(state, stream_mode="updates")
        try:
            async for update in stream:
                for name, values in update.items():
                    visited.append(name)
                    state.update(values)
                    if full_answer:
                        if name == "persist_turn":
                            return report(case, state, recorder, executor, visited)
                        continue
                    if name == "request_router" and runtime._dispatch_route(state) != "plan":
                        trace_boundary("no_tool_route", state)
                        return report(case, state, recorder, executor, visited)
                    if name == "plan_tools" and runtime._dispatch_planned(state) != "execute":
                        trace_boundary("planning_stopped", state)
                        return report(case, state, recorder, executor, visited)
                    if name == "update_subquery_ledger" and not any(
                        s["status"] in {"ready", "pending"} for s in state["task_status"].values()
                    ):
                        trace_boundary("tool_handoff", state)
                        return report(case, state, recorder, executor, visited)
        finally:
            await stream.aclose()
    raise AssertionError(f"Unexpected terminal path: {visited}")


def report(case, state, recorder, executor, visited):
    context = build_answer_context(state)
    clean = {
        k: v for k, v in state.items() if k not in {"working_memory", "working_memory_snapshot"}
    }
    memory = _next_working_memory(WorkingMemoryV2.model_validate(case.get("memory", {})), clean)
    return {
        "id": case["id"],
        "answer": state.get("answer"),
        "subquery_ledger": state.get("subquery_ledger", []),
        "input": case,
        "planning_failures": assess(case, state),
        "information_issues": inspect_results(case, state),
        "visited": visited,
        "llm1": state.get("goal_plan"),
        "llm2": state.get("execution_plan"),
        "llm_attempts": recorder.outputs,
        "tool_planner_outputs": executor.tool_model.outputs,
        "tool_calls": executor.calls,
        "tool_results": state.get("tool_results", []),
        "normalized": state.get("normalized_tool_results", []),
        "answer_context": context,
        "memory": memory.model_dump(mode="json"),
    }


def cases():
    base = json.loads(CASE_PATH.read_text())
    base.extend(
        [
            {
                "id": "alternative_budget",
                "message": "白色蓝牙键盘100元以内",
                "dispositions": ["tool_planning"],
                "tools": ["catalog_search"],
            },
            {
                "id": "missing_fact",
                "message": "查SKU 501是否支持蓝牙，必须蓝牙",
                "dispositions": ["tool_planning"],
                "tools": ["catalog_search"],
            },
            {
                "id": "missing_object",
                "message": "查SKU 999的价格",
                "dispositions": ["tool_planning"],
                "tools": ["catalog_search"],
            },
            {
                "id": "second_candidate",
                "message": "第二款现在多少钱",
                "dispositions": ["tool_planning"],
                "tools": ["catalog_search"],
                "memory": {
                    "catalog": {
                        "query_plan": {"selection_scope": "sku"},
                        "candidate_sku_ids": [101, 201],
                        "candidate_spu_ids": [11, 22],
                    }
                },
            },
        ]
    )
    return base


class ScriptedSmokeModel:
    """One deterministic offline example; not a natural-language quality test."""

    def bind_tools(self, definitions, **kwargs):
        name = definitions[0]["function"]["name"]

        class Bound:
            async def ainvoke(self, messages):
                query = "查询SKU 101价格"
                outputs = {
                    "route_request": {
                        "rewritten_query": query,
                        "goals": [{"id": "goal_1", "query": query, "source_text": query,
                                   "disposition": "tool_planning"}],
                    },
                    "submit_execution_plan": {
                        "tasks": [{"id": "task_1", "goal_id": "goal_1",
                                   "tool_name": "catalog_search", "arguments": {"query": query}}],
                    },
                }
                if name not in outputs:
                    raise AssertionError(f"Unexpected model stage: {name}")
                return AIMessage(content="", tool_calls=[
                    {"id": name, "name": name, "args": outputs[name]},
                ])

        return Bound()

    async def ainvoke(self, messages):
        raise AssertionError("Explicit SKU smoke case must not call an internal model")


def implementation_manifest():
    root = Path(__file__).resolve().parents[2]

    def git(*args):
        result = run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else "unavailable"

    files = [*sorted((root / "backend/app").rglob("*.py")),
             *sorted((root / "backend/scripts").glob("*.py")),
             *sorted((root / "tests/agent_loop").rglob("*.py")),
             *sorted((root / "evals").rglob("*.py")),
             root / "backend/uv.lock", CASE_PATH]
    return {
        "head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
        "dirty": bool(git("status", "--porcelain")),
        "versions": {name: version(name) for name in ("langgraph", "langchain-core", "langsmith")},
        "sha256": {str(p.relative_to(root)): sha256(p.read_bytes()).hexdigest() for p in files},
    }


async def main(args):
    # Scripted mode does not even load .env or construct a real model.
    settings = Settings(_env_file=None, llm_api_key="") if args.scripted else get_settings()
    if not args.scripted and not settings.llm_api_key:
        raise SystemExit("No configured model; no calls made")
    from evals.support.paths import RESULTS

    batch = RESULTS / "goal_tool_flow" / (
        "batch-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex
    )
    batch.mkdir(parents=True, mode=0o700)
    settings = settings.model_copy(update={
        "agent_trace_mode": "langsmith" if args.langsmith else "local",
        "agent_trace_dir": batch / "cases",
    })
    selected = [{"id": "scripted_sku", "message": "查询SKU 101价格",
                 "dispositions": ["tool_planning"], "tools": ["catalog_search"]}] \
        if args.scripted else cases()
    selected = [c for c in selected if not args.case or c["id"] in args.case]
    if not selected:
        raise SystemExit("No matching cases; no model calls made")
    manifest = {
        **implementation_manifest(), "cases": selected,
        "model": "scripted" if args.scripted else settings.llm_model,
        "trace_mode": settings.agent_trace_mode,
        "data_mode": "synthetic repositories; no configured database; stop before LLM3",
    }
    (batch / "manifest.json").write_text(
        json.dumps(trace_data(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    results = {
        "model": manifest["model"],
        "data_mode": (
            "synthetic repositories; real tool services/planners; lexical fixture retrieval"
        ),
        "results": [],
    }
    for case in selected:
        recording = None
        try:
            with agent_trace(settings, source="tool_flow_eval", case_id=case["id"]) as recording:
                item = await asyncio.wait_for(
                    evaluate(case, settings, model=ScriptedSmokeModel() if args.scripted else None),
                    timeout=150,
                )
        except Exception as exc:
            item = {"id": case["id"], "input": case, "error_type": type(exc).__name__}
        item["trace_directory"] = str(recording.directory.relative_to(batch)) if recording else None
        results["results"].append(item)
        (batch / "results.json").write_text(
            json.dumps(trace_data(results), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(
            case["id"],
            item.get("error_type")
            or {"planning": item["planning_failures"], "information": item["information_issues"]},
            flush=True,
        )
    # Preserve the old explicit output option as an extra export, never the only copy.
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text((batch / "results.json").read_text(), encoding="utf-8")
    (batch / "report.md").write_text(
        "# Tool-flow recording\n\n"
        + f"Model: {manifest['model']}; HEAD: {manifest['head']}; dirty: {manifest['dirty']}\n\n"
        + "Synthetic data; stops before LLM3. Existing heuristic checks are review hints.\n\n"
        + "\n".join(
            f"- {r['id']}: {r.get('error_type', 'recorded')}; trace: {r['trace_directory']}"
            for r in results["results"]
        ) + "\n", encoding="utf-8",
    )
    print(f"Artifacts: {batch}", flush=True)
    return int(any("error_type" in r for r in results["results"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--scripted", action="store_true")
    parser.add_argument("--langsmith", action="store_true", help="Also upload to LangSmith")
    parser.add_argument("--case", action="append")
    parser.add_argument("--output", type=project_path, help="Optional extra copy of results.json")
    args = parser.parse_args()
    if args.scripted and args.langsmith:
        parser.error("--scripted is strictly offline; use --live --langsmith for cloud tracing")
    raise SystemExit(asyncio.run(main(args)))
