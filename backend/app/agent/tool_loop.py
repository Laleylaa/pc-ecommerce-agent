"""Tool planning, wave validation, and terminal decision helpers."""

import copy
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.answer_context import (
    answerable_source_tool_call_ids,
    build_answer_context,
)
from app.agent.artifacts import (
    bound_task_order_id,
    ensure_task_runtime,
    ready_tasks,
    user_clarifiable_blockers,
)
from app.agent.decisions import OrchestratorDecision, PlannedToolCall, infer_tool_subquery
from app.agent.limits import TOOL_RETRY_POLICY
from app.agent.outcomes import (
    build_subquery_ledger,
    is_active_ledger_entry,
    normalize_tool_result,
    query_fingerprint,
    tool_call_fingerprint,
)
from app.agent.prompts import (
    build_orchestrator_system_prompt,
    build_orchestrator_user_prompt,
)
from app.agent.prompts.observation import build_answer_prompt
from app.agent.recovery import recovery_enabled, wave_limit
from app.agent.responses import (
    _fallback_answer,
    _fallback_unavailable_answer,
    _latest_successful_tool_output,
    _usable_tool_call_ids,
)
from app.agent.routing import (
    RoutedTask,
    tool_planning_subqueries,
    user_facing_tasks,
)
from app.agent.state import AgentState
from app.tools.contracts import LLM_SAFE_TOOL_NAMES, ToolContract


def _orchestrator_messages(
    state: AgentState,
    call_count: int,
) -> list[SystemMessage | HumanMessage | AIMessage]:
    messages: list[SystemMessage | HumanMessage | AIMessage] = [
        SystemMessage(
            content=build_orchestrator_system_prompt(
                tool_waves=state.get("tool_waves", []),
                tool_results=state.get("tool_results", []),
                answer_phase=not _planner_requires_business_tools(state),
            )
        )
    ]
    planning_phase = _planner_requires_business_tools(state)
    if not planning_phase:
        messages[0] = SystemMessage(
            content=build_answer_prompt(recovery_enabled=recovery_enabled(state)),
        )
    routed_subqueries = [
        {
            "id": item.id,
            "goal_id": item.goal_id,
            "canonical_query": item.canonical_query,
            "depends_on": item.depends_on,
            "input_requirements": [
                requirement.model_dump(mode="json") for requirement in item.input_requirements
            ],
            "produces": item.produces,
            "answer_role": item.answer_role,
            "capability": item.capability,
            "result_selector": (
                item.result_selector.model_dump(mode="json")
                if item.result_selector is not None
                else None
            ),
        }
        for item in _prompt_tool_subqueries(state)
    ]
    answer_context = None
    if not planning_phase:
        answer_context = build_answer_context(state)
        state["answer_context"] = answer_context
    messages.append(
        HumanMessage(
            content=build_orchestrator_user_prompt(
                message=None,
                tool_wave_count=state.get("tool_wave_count", 0),
                orchestrator_call_count=call_count,
                memory_context=None,
                routed_subqueries=routed_subqueries if planning_phase else None,
                subquery_ledger=None,
                task_status=None,
                task_artifacts=None,
                terminal_guard_feedback=state.get("terminal_guard_feedback"),
                answer_context=answer_context,
            )
        )
    )
    return messages


def _tool_decision(name: str, arguments: dict[str, Any]) -> OrchestratorDecision:
    return OrchestratorDecision(
        type="tool_calls",
        tool_calls=[
            PlannedToolCall(
                id=f"fallback_{name}",
                name=name,
                arguments=arguments,
                subquery=infer_tool_subquery(name, arguments),
            )
        ],
    )


def _orchestrator_business_tool_definition(contract: ToolContract) -> dict[str, Any]:
    definition = copy.deepcopy(contract.as_llm_tool())
    function = definition["function"]
    function["description"] = (
        f"{function['description']} The runtime injects the frozen routed canonical query; "
        "do not provide or rewrite a query field."
    )
    parameters = function["parameters"]
    properties = parameters.setdefault("properties", {})
    properties.pop("query", None)
    properties["subquery"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 300,
        "description": (
            "Copy the task_n ID of exactly one ready Task (legacy sq_n is accepted). Do not "
            "invent, rewrite, merge, or split routed Tasks."
        ),
    }
    required = parameters.setdefault("required", [])
    if "query" in required:
        required.remove("query")
    if "subquery" not in required:
        required.append("subquery")
    return definition


def _constrain_calls_to_route_plan(
    state: AgentState,
    calls: list[PlannedToolCall],
) -> list[PlannedToolCall]:
    routed = {item.id: item for item in _admissible_tool_subqueries(state)}
    only_routed = next(iter(routed.values())) if len(routed) == 1 else None
    constrained: list[PlannedToolCall] = []
    for call in calls:
        raw_subquery = call.subquery.strip()
        subquery = routed.get(raw_subquery)
        if (
            subquery is None
            and only_routed is not None
            and not re.fullmatch(r"(?:sq|task)_\d+", raw_subquery, flags=re.IGNORECASE)
        ):
            # When only one admitted task exists, a missing subquery ID is unambiguous. Older
            # model responses may contain an inferred natural-language label here instead of
            # the Task metadata. Explicit unknown Task IDs remain rejected.
            subquery = only_routed
        if subquery is None:
            continue
        if subquery.capability not in {"planner_required", call.name}:
            continue
        arguments = dict(call.arguments)
        # The Router owns query rewrite. Never ask the model to reproduce trusted canonical
        # text byte-for-byte; overwrite any model-provided query before public input validation.
        arguments["query"] = subquery.query
        constrained.append(
            call.model_copy(
                update={
                    "arguments": arguments,
                    "subquery": subquery.id,
                    "canonical_query": subquery.query,
                    "tool_query": subquery.query,
                }
            )
        )
    return constrained


def _unique_tool_call_ids(
    calls: list[PlannedToolCall],
    previous_waves: list[dict[str, Any]],
    call_count: int,
) -> list[PlannedToolCall]:
    used_ids = {
        str(call.get("id"))
        for wave in previous_waves
        for call in wave.get("calls", [])
        if isinstance(call, dict) and call.get("id")
    }
    normalized: list[PlannedToolCall] = []
    for index, call in enumerate(calls, start=1):
        call_id = call.id
        if not call_id or call_id in used_ids:
            base_id = f"call_{call_count}_{index}"
            call_id = base_id
            suffix = 1
            while call_id in used_ids:
                suffix += 1
                call_id = f"{base_id}_{suffix}"
        used_ids.add(call_id)
        normalized.append(call.model_copy(update={"id": call_id}))
    return normalized


def _find_reusable_tool_result(
    state: AgentState,
    call: PlannedToolCall,
) -> dict[str, Any] | None:
    fingerprint = tool_call_fingerprint(call.name, call.arguments)
    task_id = str(call.subquery or "").strip()
    matches: list[dict[str, Any]] = []
    for wave in state.get("tool_waves", []):
        calls = {
            str(previous.get("id") or ""): previous
            for previous in wave.get("calls", [])
            if isinstance(previous, dict)
        }
        for result in wave.get("results", []):
            if not isinstance(result, dict) or result.get("name") != call.name:
                continue
            previous_call = calls.get(str(result.get("tool_call_id") or ""), {})
            if str(previous_call.get("subquery") or "").strip() != task_id:
                continue
            previous_arguments = previous_call.get("arguments", {})
            previous_fingerprint = str(previous_call.get("fingerprint") or "")
            if not previous_fingerprint and isinstance(previous_arguments, dict):
                previous_fingerprint = tool_call_fingerprint(call.name, previous_arguments)
            if previous_fingerprint == fingerprint:
                matches.append(result)

    if not matches:
        return None

    outcomes = [normalize_tool_result(result) for result in matches]
    if outcomes[-1].outcome != "error":
        return matches[-1]
    # Timeout attempts share the executor's policy, including when retries are disabled.
    error = matches[-1].get("execution", {}).get("error") or {}
    max_attempts = TOOL_RETRY_POLICY.max_attempts if error.get("code") == "timeout" else 2
    if sum(outcome.outcome == "error" for outcome in outcomes) >= max_attempts:
        return matches[-1]
    return None


def _planner_requires_business_tools(state: AgentState) -> bool:
    """Return whether an observation turn can still make an admissible business call."""
    if state.get("tool_wave_count", 0) >= wave_limit(state):
        return False
    if state.get("route_plan") and (
        "task_status" not in state or "task_artifacts" not in state
    ):
        ensure_task_runtime(state)

    if _ready_unattempted_tool_subqueries(state):
        return True

    return False


def _deterministic_recovery_decision(state: AgentState) -> OrchestratorDecision | None:
    """Compile the only automatic recovery: one byte-equivalent retry when allowed."""
    if state.get("tool_wave_count", 0) >= wave_limit(state):
        return None
    ledger = [
        entry
        for entry in state.get("subquery_ledger", [])
        if isinstance(entry, dict)
        and is_active_ledger_entry(entry)
        and entry.get("outcome") == "error"
    ]
    for entry in reversed(ledger):
        call_id = str(entry.get("tool_call_id") or "")
        result = _tool_result_by_call_id(state, call_id)
        error = result.get("execution", {}).get("error", {}) if result else {}
        if not isinstance(error, dict):
            continue
        code = str(error.get("code") or "execution_error")
        action = str(error.get("recommended_action") or "")
        if not action:
            action = "retry_once" if code == "timeout" else "stop"
        if action != "retry_once":
            continue
        fingerprint = str(entry.get("fingerprint") or "")
        failures = sum(
            item.get("outcome") == "error" and str(item.get("fingerprint") or "") == fingerprint
            for item in state.get("subquery_ledger", [])
            if isinstance(item, dict)
        )
        if not TOOL_RETRY_POLICY.allows_retry(
            completed_attempts=failures, code=code,
            retryable=error.get("retryable") is True, action=action,
        ):
            continue
        return OrchestratorDecision(
            type="tool_calls",
            reason="deterministic_retry_once",
            tool_calls=[
                PlannedToolCall(
                    id=f"retry_{call_id}",
                    name=str(entry.get("tool_name") or ""),
                    arguments=dict(entry.get("arguments") or {}),
                    subquery=str(entry.get("subquery") or ""),
                    canonical_query=str(entry.get("canonical_query") or ""),
                    tool_query=str((entry.get("arguments") or {}).get("query") or ""),
                )
            ],
        )
    return None


def _ready_unattempted_tool_subqueries(state: AgentState) -> list[RoutedTask]:
    return ready_tasks(state)


def _admissible_tool_subqueries(state: AgentState) -> list[RoutedTask]:
    ready = _ready_unattempted_tool_subqueries(state)
    recoverable_ids = {
        str(entry.get("subquery") or "").strip()
        for entry in state.get("subquery_ledger", [])
        if is_active_ledger_entry(entry) and entry.get("status") in {"failed", "needs_replan"}
    }
    recoverable_ids.update(
        str(entry.get("subquery") or "").strip()
        for entry in state.get("subquery_ledger", [])
        if is_active_ledger_entry(entry)
        and entry.get("tool_name") == "order_lookup"
        and entry.get("result_type") == "order_candidates"
    )
    by_id = {item.id: item for item in tool_planning_subqueries(state.get("route_plan"))}
    return [*ready, *(by_id[item_id] for item_id in recoverable_ids if item_id in by_id)]


def _prompt_tool_subqueries(state: AgentState) -> list[RoutedTask]:
    if _planner_requires_business_tools(state):
        return _ready_unattempted_tool_subqueries(state)
    return tool_planning_subqueries(state.get("route_plan"))


def _all_planned_subqueries_usable(state: AgentState) -> bool:
    """Return whether every user-facing Task has an answerable semantic result."""
    context = build_answer_context(state)
    return bool(context["tasks"]) and context["completion"] == "full"


def _followup_tool_call_allowed(state: AgentState, call: PlannedToolCall) -> bool:
    """Admit only same-tool recovery or an original-request dependent call."""
    ledger = state.get("subquery_ledger", [])
    if not ledger and state.get("tool_waves"):
        ledger = [
            entry.model_dump(mode="json")
            for entry in build_subquery_ledger(state.get("tool_waves", []))
        ]

    subquery = (call.subquery or infer_tool_subquery(call.name, call.arguments)).strip()
    key = subquery.casefold()
    matching = [
        entry
        for entry in ledger
        if is_active_ledger_entry(entry)
        and str(entry.get("subquery") or "").strip().casefold() == key
    ]
    if not matching:
        if state.get("route_plan") and not _call_targets_ready_task(state, call):
            return False
        return _is_supported_dependent_call(state, call)

    latest = matching[-1]
    if query_fingerprint(call.arguments.get("query")) != str(
        latest.get("query_fingerprint")
        or query_fingerprint(
            latest.get("canonical_query") or latest.get("arguments", {}).get("query")
        )
    ):
        return False

    outcome = str(latest.get("outcome") or "")
    if outcome == "usable" and _is_supported_dependent_call(state, call):
        # A usable discovery result may be the prerequisite for a different Tool in the same
        # routed task, for example an order list followed by order details. Do not mark the whole
        # subquery complete before admitting that explicitly requested dependent step.
        return True
    if outcome in {"usable", "empty", "not_found", "unsupported", "insufficient"}:
        return False

    previous_fingerprint = str(latest.get("fingerprint") or "")
    next_fingerprint = tool_call_fingerprint(call.name, call.arguments)
    if outcome != "error":
        return False

    result = _tool_result_by_call_id(state, str(latest.get("tool_call_id") or ""))
    error = result.get("execution", {}).get("error", {}) if result else {}
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or "execution_error")
    action = str(error.get("recommended_action") or "")
    if not action:
        action = {
            "invalid_input": "replan_arguments",
            "timeout": "retry_once",
        }.get(code, "stop")

    if action == "retry_once":
        same_failures = sum(
            entry.get("outcome") == "error"
            and str(entry.get("fingerprint") or "") == previous_fingerprint
            for entry in ledger
        )
        return TOOL_RETRY_POLICY.allows_retry(
            completed_attempts=same_failures, code=code,
            retryable=error.get("retryable") is True, action=action,
        ) and next_fingerprint == previous_fingerprint
    if action == "replan_arguments":
        if call.name != str(latest.get("tool_name") or ""):
            return False
        attempts = sum(
            entry.get("outcome") == "error"
            and str(entry.get("subquery") or "").strip().casefold() == key
            for entry in ledger
        )
        return attempts < 2 and next_fingerprint != previous_fingerprint
    return False


def _is_supported_dependent_call(state: AgentState, call: PlannedToolCall) -> bool:
    """Allow only bounded continuations authorized by a ready routed Task."""
    if call.name == "order_lookup":
        task = _ready_task_for_call(state, call)
        if task is not None:
            bound_order_id = bound_task_order_id(state, task)
            return (
                bound_order_id is not None
                and call.arguments.get("order_id") == bound_order_id
            )
        if state.get("route_plan"):
            return False
        output = _latest_successful_tool_output(state, "order_lookup")
        if not output or output.get("result_type") != "order_candidates":
            return False
        candidate_ids = {
            item.get("id")
            for item in output.get("candidates", [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and not isinstance(item.get("id"), bool)
        }
        return call.arguments.get("order_id") in candidate_ids
    return False


def _call_targets_ready_task(state: AgentState, call: PlannedToolCall) -> bool:
    return _ready_task_for_call(state, call) is not None


def _ready_task_for_call(
    state: AgentState,
    call: PlannedToolCall,
) -> RoutedTask | None:
    ready = {item.id: item for item in _ready_unattempted_tool_subqueries(state)}
    task = ready.get(call.subquery.strip())
    if task is None:
        return None
    if task.capability not in {None, "planner_required", call.name}:
        return None
    if query_fingerprint(call.arguments.get("query")) != query_fingerprint(task.query):
        return None
    return task


def _tool_result_by_call_id(state: AgentState, call_id: str) -> dict[str, Any] | None:
    for result in reversed(state.get("tool_results", [])):
        if str(result.get("tool_call_id") or "") == call_id:
            return result
    return None


def _unresolved_initial_subqueries(state: AgentState) -> list[str]:
    statuses = state.get("task_status", {})
    return [
        item.canonical_query
        for item in user_facing_tasks(state.get("route_plan"))
        if statuses.get(item.id, {}).get("status") != "succeeded"
    ]


def _clarification_decision(response: str, reason: str) -> OrchestratorDecision:
    return OrchestratorDecision(
        type="clarification",
        response=response,
        reason=reason,
        control_action="ask_clarification",
    )


def _plain_text_observation_decision(
    state: AgentState,
    message: AIMessage,
) -> OrchestratorDecision | None:
    """Recover a customer answer when an observation model omits the control Tool Call."""
    if message.tool_calls or not state.get("tool_results"):
        return None
    response = _ai_message_text(message).strip()
    usable_ids = _usable_tool_call_ids(state)
    if not response or not usable_ids:
        return None

    unresolved = _unresolved_initial_subqueries(state)
    if unresolved:
        return OrchestratorDecision(
            type="partial_response",
            response=response,
            reason="plain_text_observation_recovery",
            control_action="finish_partial",
            used_tool_call_ids=usable_ids,
            unavailable_parts=unresolved,
        )
    return OrchestratorDecision(
        type="grounded_response",
        response=response,
        reason="plain_text_observation_recovery",
        control_action="finish_answer",
        used_tool_call_ids=usable_ids,
    )


def _ai_message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _state_terminal_decision(state: AgentState, reason: str) -> OrchestratorDecision:
    """Stop the loop without discarding observations already useful to the user."""
    if state.get("route_plan"):
        from app.agent.responses import fallback_goal_response

        payload = fallback_goal_response(state)
        context = build_answer_context(state)
        if payload["used_tool_call_ids"]:
            action = "finish_partial" if payload["unavailable_parts"] else "finish_answer"
            kind = "partial_response" if payload["unavailable_parts"] else "grounded_response"
        elif context["recommended_control_action"] == "ask_clarification":
            action, kind = "ask_clarification", "clarification"
        else:
            action, kind = "finish_unavailable", "unavailable_response"
            payload["unavailable_parts"] = payload["unavailable_parts"] or ["本轮回答"]
        return OrchestratorDecision(type=kind, control_action=action, reason=reason, **payload)
    answer_context = build_answer_context(state)
    has_task_context = bool(answer_context["tasks"])
    answerable_ids = (
        answerable_source_tool_call_ids(state)
        if has_task_context
        else _usable_tool_call_ids(state)
    )
    unresolved = (
        list(answer_context["unavailable_parts"])
        if has_task_context
        else _unresolved_initial_subqueries(state)
    )
    if answerable_ids:
        has_positive_facts = not has_task_context or any(
            task["semantic_outcome"] == "answered_with_facts" for task in answer_context["tasks"]
        )
        response = (
            _fallback_answer(state)
            if has_positive_facts
            else _fallback_unavailable_answer(state)
        )
        if unresolved:
            response = (
                f"{response}\n\n"
                f"暂时未能完成：{'、'.join(unresolved)}。"
                "你可以稍后重试或补充更具体的信息。"
            )
            return OrchestratorDecision(
                type="partial_response",
                response=response,
                reason=reason,
                control_action="finish_partial",
                used_tool_call_ids=answerable_ids,
                unavailable_parts=unresolved,
            )
        return OrchestratorDecision(
            type="grounded_response",
            response=response,
            reason=reason,
            control_action="finish_answer",
            used_tool_call_ids=answerable_ids,
        )
    if state.get("tool_results"):
        return OrchestratorDecision(
            type="unavailable_response",
            response=_fallback_unavailable_answer(state),
            reason=reason,
            control_action="finish_unavailable",
            unavailable_parts=_unresolved_initial_subqueries(state) or ["请求所需的业务信息"],
        )
    blockers = user_clarifiable_blockers(state)
    if blockers:
        missing = blockers[0].get("missing_information") or ["具体商品或订单信息"]
        return _clarification_decision(
            f"请补充{str(missing[0])}，我再继续查询。",
            reason,
        )
    return OrchestratorDecision(
        type="unavailable_response",
        response="当前无法可靠完成这项查询，请稍后重试。",
        reason=reason,
        control_action="finish_unavailable",
        unavailable_parts=_unresolved_initial_subqueries(state) or ["请求所需的业务信息"],
    )


def _terminal_fallback_decision(
    state: AgentState,
    validation_reason: str,
) -> OrchestratorDecision:
    reason = f"terminal_guard_fallback:{validation_reason}"
    return _state_terminal_decision(state, reason)


def _tag_from_decision(
    decision: OrchestratorDecision,
    current_tag: str | None,
) -> str:
    if decision.type == "clarification":
        return current_tag or "clarification"

    tool_names = list(dict.fromkeys(call.name for call in decision.tool_calls))
    if tool_names:
        previous_tool_names = (
            current_tag.split(" + ")
            if current_tag
            and all(self_name in LLM_SAFE_TOOL_NAMES for self_name in current_tag.split(" + "))
            else []
        )
        return " + ".join(dict.fromkeys([*previous_tool_names, *tool_names]))
    return current_tag or "general"
