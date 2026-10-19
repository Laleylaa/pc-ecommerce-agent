"""One bounded recovery batch, scoped planning and deterministic state merges.

Models suggest semantic corrections. Runtime owns admission, budgets, identities,
invalidation and execution; it does not treat a suggestion as proof of correctness.
"""

import asyncio
from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agent.limits import MAX_TOOL_WAVES

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
RECOVERY_SECONDS = 90.0
FINAL_ANSWER_RESERVE = 30.0
RECOVERY_TOOL_WAVES = 2
EXHAUSTED_REASONS = {
    "planning_output_invalid", "planning_call_timeout", "planning_deadline_exceeded",
    "invalid_plan", "timeout", "execution_error", "forbidden", "unauthorized",
    "web_search_timeout", "web_search_not_configured", "dependency_unavailable",
    "invalid_catalog_plan", "invalid_order_plan", "planning_transport_failed",
    "planning_review_inconclusive", "order_confirmation_required",
}


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str | None = Field(default=None, pattern=r"^goal_[1-9][0-9]*$")
    source_text: Text
    task_ids: list[str] = Field(default_factory=list, max_length=8)
    target_stage: Literal["llm1", "llm2"]
    evidence: Text
    hint: Text


class RecoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: list[RecoveryRequest] = Field(min_length=1, max_length=8)


def recovery_enabled(state: dict) -> bool:
    return bool(state.get("message") and state.get("route_plan")) and not (
        state.get("recovery_batch") or state.get("planning_error") or state.get("input_rule_id")
    )


def wave_limit(state: dict) -> int:
    batch = state.get("recovery_batch", {})
    return batch.get("wave_limit", MAX_TOOL_WAVES)


def answer_limit(state: dict, initial: int) -> int:
    batch = state.get("recovery_batch")
    return min(initial + 2, batch["answer_count_at_start"] + 2) if batch else initial


def recovery_context(state: dict, stage: str) -> dict | None:
    batch = state.get("recovery_batch", {})
    if batch.get("phase") != stage:
        return None
    goal_ids = batch[f"{stage}_goal_ids"]
    return {
        "goals": [g for g in state["route_plan"]["subqueries"] if g["id"] in goal_ids],
        "feedback": [i for i in batch["items"] if i["goal_id"] in goal_ids],
        "available_goal_ids": batch.get("available_goal_ids", []),
        "protected_tasks": [t for t in batch["protected_tasks"] if t["goal_id"] in goal_ids],
        "readonly_tasks": [t for t in batch["protected_tasks"] if t["goal_id"] not in goal_ids],
        "previous_tasks": [t for t in batch["previous_plan"].get("tasks", [])
                           if t["goal_id"] in goal_ids],
        "previous_results": [t for t in batch.get("previous_results", [])
                             if t["goal_id"] in goal_ids],
        "available_results": state.get("task_artifacts", {}),
    }


def start_stage(state: dict, stage: str) -> None:
    batch = state["recovery_batch"]
    for item in batch["items"]:
        if item["goal_id"] in batch[f"{stage}_goal_ids"] and item["status"] != "settled":
            item["status"] = "planning"


def admit_recovery(state: dict, requests: list[dict]) -> None:
    from app.agent.answer_context import build_answer_context

    allowed = recovery_enabled(state)
    context = build_answer_context(state)
    now = asyncio.get_running_loop().time()
    deadline = min(now + RECOVERY_SECONDS, state.get("turn_deadline_monotonic", now + 240)
                   - FINAL_ANSWER_RESERVE)
    goals = {g["id"]: g for g in state["route_plan"]["subqueries"]}
    tasks = {t["id"]: t for t in state.get("execution_plan", {}).get("tasks", [])}
    results = {t["task_id"]: t for t in [*context["tasks"], *context["supporting_tasks"]]}
    items, affected, seen = [], set(), set()
    next_goal = max([int(g.rsplit("_", 1)[1]) for g in goals] or [0]) + 1
    for raw in requests:
        request = RecoveryRequest.model_validate(raw).model_dump()
        goal_id = request["goal_id"]
        rejected = ""
        if not allowed or deadline <= now:
            rejected = "recovery_not_available"
        elif request["source_text"] not in state["message"]:
            rejected = "source_not_in_user_message"
        elif goal_id is None:
            if request["target_stage"] != "llm1" or request["task_ids"] or len(goals) >= 8:
                rejected = "invalid_missing_goal_request"
            elif any(
                g["disposition"] in {"security_refusal", "human_handoff"}
                and request["source_text"] in (g.get("source_text") or g["query"])
                for g in goals.values()
            ):
                rejected = "protected_business_boundary"
            elif any(request["source_text"] == g.get("source_text") for g in goals.values()):
                rejected = "source_already_has_goal"
            else:
                goal_id = f"goal_{next_goal}"
                next_goal += 1
                goals[goal_id] = {
                    "id": goal_id, "query": request["source_text"],
                    "source_text": request["source_text"], "disposition": "clarification",
                    "reason_code": "recovery_missing_goal", "tasks": [],
                    "missing_information": ["尚未确认该请求的处理方式"],
                }
        elif goal_id not in goals or goal_id in seen:
            rejected = "unknown_or_duplicate_goal"
        elif goals[goal_id]["disposition"] in {"security_refusal", "human_handoff"}:
            rejected = "protected_business_boundary"
        elif request["source_text"] not in (goals[goal_id].get("source_text") or ""):
            rejected = "source_outside_goal"
        elif request["target_stage"] == "llm2" and goals[goal_id]["disposition"] != "tool_planning":
            rejected = "goal_requires_llm1"
        selected = set(request["task_ids"])
        if not rejected:
            owned = {t["id"] for t in tasks.values() if t["goal_id"] == goal_id}
            if not selected <= owned or len(selected) != len(request["task_ids"]):
                rejected = "invalid_task_scope"
            elif request["target_stage"] == "llm1":
                selected = owned
            elif not selected:
                selected = {t for t in owned if not results.get(t, {}).get("resolved")}
            if any(results.get(t, {}).get("result", {}).get("reason_code") in EXHAUSTED_REASONS
                   for t in selected):
                rejected = "exhausted_or_forbidden_call"
        request.update(goal_id=goal_id, status="settled" if rejected else "pending",
                       reason=rejected)
        items.append(request)
        if not rejected:
            seen.add(goal_id)
            affected.update(selected)
    # Invalidate the transitive consumers, including consumers in other goals.
    while True:
        consumers = {t["id"] for t in tasks.values() if set(t["depends_on"]) & affected}
        if consumers <= affected:
            break
        affected |= consumers
    for identity in sorted(affected):
        goal_id = tasks[identity]["goal_id"]
        if goal_id not in seen:
            seen.add(goal_id)
            items.append({"goal_id": goal_id, "task_ids": [], "target_stage": "llm2",
                          "source_text": goals[goal_id].get("source_text", ""),
                          "evidence": "上游结果将被替换", "hint": "重新绑定已修正的上游任务",
                          "status": "pending", "reason": "dependency_invalidated"})
    accepted = [i for i in items if i["status"] != "settled"]
    batch = {
        "id": 1, "phase": "llm1" if any(i["target_stage"] == "llm1" for i in accepted)
        else "llm2" if accepted else "done",
        "items": items, "affected_task_ids": sorted(affected),
        "answer_count_at_start": state.get("answer_call_count", 0),
        "llm1_goal_ids": [i["goal_id"] for i in accepted if i["target_stage"] == "llm1"],
        "llm2_goal_ids": [i["goal_id"] for i in accepted if i["target_stage"] == "llm2"],
        "protected_tasks": [deepcopy(t) for t in tasks.values() if t["id"] not in affected],
        "previous_plan": deepcopy(state.get("execution_plan", {})),
        "previous_route": deepcopy(state["route_plan"]),
        "previous_results": deepcopy(list(results.values())),
        "previous_artifacts": {
            t: deepcopy(state.get("task_artifacts", {}).get(t)) for t in affected
        },
        "available_goal_ids": [f"goal_{i}" for i in range(next_goal, next_goal + 8 - len(goals))],
        "deadline": deadline, "wave_limit": min(
            MAX_TOOL_WAVES + RECOVERY_TOOL_WAVES,
            state.get("tool_wave_count", 0) + RECOVERY_TOOL_WAVES,
        ),
        "blocked_task_ids": sorted(affected), "replacements": {},
    }
    state["recovery_batch"] = batch
    state["route_plan"]["subqueries"] = list(goals.values())
    for identity in affected:
        state.get("task_artifacts", {}).pop(identity, None)
    mark_superseded(state)
    from app.agent.projections import rebuild_tool_projections

    rebuild_tool_projections(state)


def mark_superseded(state: dict) -> None:
    affected = set(state.get("recovery_batch", {}).get("affected_task_ids", []))
    for entry in state.get("subquery_ledger", []):
        if entry.get("subquery_id") in affected or entry.get("subquery") in affected:
            entry["status"] = "superseded"


def validate_route_scope(plan: Any, context: dict) -> None:
    from app.agent.goal_planning import PlanValidationError

    required = {g["id"] for g in context["goals"]}
    ids = {g.id for g in plan.goals}
    sources = [g.get("source_text") or g["query"] for g in context["goals"]]
    if not required <= ids or not ids <= required | set(context["available_goal_ids"]):
        raise PlanValidationError("recovery must preserve selected goal IDs; no unrelated goals")
    if any(not any(g.source_text in source for source in sources) for g in plan.goals):
        raise PlanValidationError("recovery source_text is outside approved goals")


def merge_route(state: dict, route: Any) -> None:
    batch = state["recovery_batch"]
    selected = set(batch["llm1_goal_ids"])
    goals = [g for g in state["route_plan"]["subqueries"] if g["id"] not in selected]
    goals.extend(g.model_dump(mode="json") for g in route.subqueries)
    state["route_plan"] = {"rewritten_query": "；".join(g["query"] for g in goals),
                           "subqueries": goals}
    state["rewritten_query"] = state["route_plan"]["rewritten_query"]
    for goal in route.subqueries:
        if not any(item["goal_id"] == goal.id for item in batch["items"]):
            batch["items"].append({"goal_id": goal.id, "target_stage": "llm1",
                                   "status": "pending", "reason": "goal_split"})
        if goal.disposition == "tool_planning":
            batch["llm2_goal_ids"].append(goal.id)
        else:
            for item in batch["items"]:
                if item["goal_id"] == goal.id:
                    item.update(status="settled", reason="goal_reclassified")
    # Old tasks of reinterpreted goals are removed; their history stays in the batch/ledger.
    state.setdefault("execution_plan", {})["tasks"] = [
        t for t in state.get("execution_plan", {}).get("tasks", []) if t["goal_id"] not in selected
    ]
    state["execution_plan"]["blocked_goals"] = [
        g for g in state["execution_plan"].get("blocked_goals", []) if g["goal_id"] not in selected
    ]
    batch["phase"] = "llm2" if batch["llm2_goal_ids"] else "done"


def merged_execution(state: dict, plan: Any) -> Any:
    from app.agent.goal_planning import ExecutionPlan, PlanValidationError

    batch = state["recovery_batch"]
    selected = set(batch["llm2_goal_ids"])
    if any(t.goal_id not in selected for t in plan.tasks) or any(
        g.goal_id not in selected for g in plan.blocked_goals
    ):
        raise PlanValidationError("recovery cannot modify unrelated goals")
    submitted = {t.id: t.model_dump(mode="json") for t in plan.tasks}
    protected = {t["id"]: t for t in batch["protected_tasks"]}
    for identity, original in protected.items():
        if original["goal_id"] in selected and submitted.get(identity) != original:
            raise PlanValidationError("recovery must preserve protected tasks exactly")
        if identity in submitted and submitted[identity] != original:
            raise PlanValidationError("recovery task ID collides with protected task")
    old = batch["previous_plan"].get("tasks", [])
    affected = set(batch["affected_task_ids"])
    changed = [t for t in plan.tasks if t.id not in protected]
    # A changed dependency permits a consumer to retain its question/arguments.
    def signature(t):
        return (t["tool_name"], t["arguments"], t.get("depends_on", []), t.get("bindings", []))
    for task in changed:
        if not set(task.depends_on) & affected and any(
            signature(task.model_dump(mode="json")) == signature(t)
            for t in old if t["id"] in affected
        ):
            raise PlanValidationError("recovery cannot repeat the same failed/invalidated call")
    merged = ExecutionPlan(
        tasks=[t for t in state.get("execution_plan", {}).get("tasks", [])
               if t["goal_id"] not in selected] + list(plan.tasks),
        blocked_goals=[g for g in state.get("execution_plan", {}).get("blocked_goals", [])
                       if g["goal_id"] not in selected] + list(plan.blocked_goals),
    )
    return merged


def install_execution(state: dict, plan: Any) -> None:
    from app.agent.routing import RoutedTask

    batch = state["recovery_batch"]
    protected = {t["id"] for t in batch["protected_tasks"]}
    old_ids = {t["id"] for t in batch["previous_plan"].get("tasks", [])}
    next_id = max([int(t.rsplit("_", 1)[1]) for t in old_ids] or [0]) + 1
    mapping = {}
    for task in plan.tasks:
        if task.id not in protected:
            mapping[task.id] = f"task_{next_id}"
            next_id += 1
    for task in plan.tasks:
        task.id = mapping.get(task.id, task.id)
        task.depends_on = [mapping.get(t, t) for t in task.depends_on]
        for binding in task.bindings:
            if binding.source == "task_output":
                binding.ref = mapping.get(binding.ref, binding.ref)
    batch["replacements"] = mapping
    batch["new_task_ids"] = list(mapping.values())
    state["execution_plan"] = plan.model_dump(mode="json")
    blocked = {g.goal_id for g in plan.blocked_goals}
    for goal in state["route_plan"]["subqueries"]:
        if goal["id"] not in batch["llm2_goal_ids"]:
            continue
        goal["planning_pending"] = goal["id"] in blocked
        goal["tasks"] = [RoutedTask(
            id=t.id, goal_id=t.goal_id, canonical_query=t.arguments.get("query") or goal["query"],
            capability=t.tool_name, depends_on=t.depends_on, produces="result",
            answer_role=t.answer_role,
        ).model_dump(mode="json") for t in plan.tasks if t.goal_id == goal["id"]]
    active_ids = {t.id for t in plan.tasks}
    state["task_status"] = {
        k: v for k, v in state.get("task_status", {}).items() if k in active_ids
    }
    state["task_artifacts"] = {k: v for k, v in state.get("task_artifacts", {}).items()
                               if k in protected and k in active_ids}
    batch["phase"] = "executing"
    for item in batch["items"]:
        if item["status"] != "settled":
            item["status"] = "settled" if item["goal_id"] in blocked else "executing"
            if item["goal_id"] in blocked:
                item["reason"] = "planning_blocked"


def settle_batch(state: dict) -> None:
    batch = state.get("recovery_batch", {})
    if not batch or batch["phase"] in {"llm1", "llm2"}:
        return
    batch["phase"] = "done"
    for item in batch["items"]:
        if item["status"] != "settled":
            item.update(status="settled", reason="execution_settled")


def repeats_invalidated_call(
    state: dict, name: str, arguments: dict, input_model: type[BaseModel] | None = None,
) -> bool:
    from app.agent.outcomes import tool_call_fingerprint

    affected = set(state.get("recovery_batch", {}).get("affected_task_ids", []))
    def fingerprint(value):
        if input_model is not None:
            try:
                value = input_model.model_validate(value).model_dump(mode="json")
            except ValueError:
                pass  # Invalid previous inputs remain distinct from corrected valid inputs.
        return tool_call_fingerprint(name, value)

    current = fingerprint(arguments)
    return any(
        call.get("subquery") in affected and call.get("name") == name
        and fingerprint(call.get("arguments", {})) == current
        for wave in state.get("tool_waves", []) for call in wave.get("calls", [])
    )


def fail_stage(state: dict, stage: str, reason: str) -> None:
    batch = state["recovery_batch"]
    selected = set(batch[f"{stage}_goal_ids"])
    for item in batch["items"]:
        if item["goal_id"] in selected and item["status"] != "settled":
            item.update(status="settled", reason=reason)
    batch["phase"] = "llm2" if stage == "llm1" and batch["llm2_goal_ids"] else "done"
