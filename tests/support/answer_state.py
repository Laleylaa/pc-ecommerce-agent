"""Tool output -> task state -> LLM3, with scripted models and no external I/O."""


from app.agent.legacy.artifacts import (
    extract_wave_artifacts,
    initialize_task_runtime,
    refresh_task_status,
)
from app.agent.legacy.outcomes import build_subquery_ledger


def result_state(name, output=None, error=None, *, waves=1, dependency=False):
    """Exercise actual normalization/artifact/readiness, without constructing a service."""
    execution = {
        "tool_name": name, "ok": error is None, "output": output, "error": error,
    }
    result = {"tool_call_id": "call_1", "name": name, "execution": execution}
    tasks = [{
        "id": "task_1", "goal_id": "goal_1", "canonical_query": "查询当前条件下的结果",
        "capability": name, "produces": "result", "answer_role": "user_facing",
    }]
    if dependency:
        tasks.append({
            "id": "task_2", "goal_id": "goal_1", "canonical_query": "列出这些商品的规格",
            "capability": "catalog_facets", "produces": "result", "depends_on": ["task_1"],
            "answer_role": "user_facing",
        })
    state = {
        "route_plan": {"rewritten_query": "查询当前条件下的结果", "subqueries": [{
            "id": "goal_1", "query": "查询当前条件下的结果", "disposition": "tool_planning",
            "reason_code": "test", "tasks": tasks,
        }]},
        "tool_wave_count": waves,
        "tool_results": [result],
        "tool_waves": [{"wave": waves, "calls": [{
            "id": "call_1", "name": name, "subquery": "task_1",
            "arguments": {"query": "查询当前条件下的结果"},
        }], "results": [result]}],
    }
    initialize_task_runtime(state)
    extract_wave_artifacts(state)
    state["subquery_ledger"] = [
        entry.model_dump(mode="json") for entry in build_subquery_ledger(state["tool_waves"])
    ]
    refresh_task_status(state)
    return state
