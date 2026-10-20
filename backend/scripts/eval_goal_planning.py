"""Small live LLM1/LLM2 evaluation. Synthetic input; no tools, DB, or persistence.

Run with --live to spend model quota. Reports include plans, not credentials or chat data.
"""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.agent.graph import AgentRuntime
from app.core.config import get_settings
from app.core.llm import build_chat_model
from app.schemas.chat import ChatRequest
from app.schemas.context import WorkingMemoryV2

CASE_PATH = Path(__file__).resolve().parents[1] / "evals/goal_planning/cases.json"


class NoIO:
    def __getattr__(self, name):
        raise AssertionError(f"forbidden business I/O: {name}")


class RecordingModel:
    def __init__(self, model):
        self.model = model
        self.outputs = []
        self.errors = []

    async def ainvoke(self, messages):
        return await self.model.ainvoke(messages)

    def bind_tools(self, definitions, **kwargs):
        bound = self.model.bind_tools(definitions, **kwargs)
        parent = self

        class Bound:
            async def ainvoke(self, messages):
                try:
                    response = await bound.ainvoke(messages)
                except Exception as exc:
                    parent.errors.append(type(exc).__name__)
                    raise
                parent.outputs.append(
                    {
                        "tool_calls": response.tool_calls,
                        "invalid_tool_calls": response.invalid_tool_calls,
                        "content": response.content,
                    }
                )
                return response

        return Bound()


def assess(case: dict, state: dict) -> list[str]:
    failures = []
    expected = Counter(case["dispositions"])
    # Check raw LLM1 AND runtime route so guards cannot mask model mistakes.
    for stage, goals in (
        ("llm1", state.get("goal_plan", {}).get("goals", [])),
        ("runtime", state.get("route_plan", {}).get("subqueries", [])),
    ):
        actual = Counter(g["disposition"] for g in goals)
        accepted = [expected, *[Counter(x) for x in case.get("disposition_alternatives", [])]]
        good = any(
            set(actual) == set(option) if case.get("flex_goal_count") else actual == option
            for option in accepted
        )
        if not good:
            failures.append(f"{stage}: dispositions {dict(actual)} != {dict(expected)}")
        combined = " ".join(g["query"] for g in goals)
        for word in case.get("preserve", []):
            if word.casefold() not in combined.casefold():
                failures.append(f"{stage}: missing {word}")
    tasks = state.get("execution_plan", {}).get("tasks", [])
    actual_tools = Counter(t["tool_name"] for t in tasks)
    if not any(
        actual_tools == Counter(option)
        for option in [case["tools"], *case.get("tool_alternatives", [])]
    ):
        failures.append(f"tools: {dict(actual_tools)} != {dict(Counter(case['tools']))}")
    queries = " ".join(t["arguments"].get("query", "") for t in tasks)
    for word in case.get("preserve", []):
        if tasks and word.casefold() not in queries.casefold():
            failures.append(f"llm2: missing {word}")
    for word in case.get("forbid", []):
        if word in queries:
            failures.append(f"llm2: obsolete condition {word}")
    if case["id"].startswith("office"):
        search_queries = " ".join(
            t["arguments"]["query"] for t in tasks if t["tool_name"] == "catalog_search"
        )
        scene = "办公室" if "办公室" in case["message"] else "工位"
        if scene not in search_queries:
            failures.append("llm2: search lost usage context")
        for invented in ("静音", "RGB"):
            if invented in queries:
                failures.append(f"llm2: invented condition {invented}")
    if state.get("planning_error"):
        failures.append("planning_error (see attempt outputs; no transport details recorded)")
    return failures


async def evaluate(case: dict, settings: Any) -> dict:
    recorder = RecordingModel(build_chat_model(settings, temperature=0, max_retries=0))
    runtime = AgentRuntime(
        None,
        settings,
        chat_model=recorder,
        router_model=recorder,
        tool_executor=NoIO(),
        context_service=NoIO(),
    )
    state = runtime._initial_state(ChatRequest(message=case["message"]), 1)
    state.update(
        history=case.get("history", []),
        memory=[],
        working_memory_snapshot=WorkingMemoryV2.model_validate(case.get("memory", {})).model_dump(
            mode="json"
        ),
    )
    await runtime._request_route(state)
    if runtime._dispatch_route(state) == "plan":
        await runtime._plan_tools(state)
    return {
        "id": case["id"],
        "input": case,
        "failures": assess(case, state),
        "llm1": state.get("goal_plan"),
        "runtime_route": state.get("route_plan"),
        "llm2": state.get("execution_plan"),
        "attempt_outputs": recorder.outputs,
        "transport_errors": recorder.errors,
        "llm1_calls": state.get("request_router_call_count", 0),
        "llm2_calls": state.get("task_planner_call_count", 0),
    }


async def main(args):
    settings = get_settings()
    if not settings.llm_api_key:
        raise SystemExit("Configured model key unavailable; no calls made")
    cases = json.loads(CASE_PATH.read_text())
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
        if not cases:
            raise SystemExit("No selected cases")
    report = {"model": settings.llm_model, "business_tool_calls": 0, "results": []}
    for case in cases:
        try:
            result = await asyncio.wait_for(evaluate(case, settings), timeout=90)
        except Exception as exc:
            result = {"id": case["id"], "failures": [type(exc).__name__]}
        report["results"].append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(case["id"], "FAIL" if result["failures"] else "PASS", flush=True)
    failed = sum(bool(r["failures"]) for r in report["results"])
    print(f"{len(cases) - failed}/{len(cases)} passed; report: {args.output}")
    return bool(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable paid model calls")
    parser.add_argument("--case", action="append")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("--live is required; no model call has been made")
    raise SystemExit(asyncio.run(main(arguments)))
