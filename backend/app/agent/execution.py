"""Business Tool wave execution and persistence."""

import asyncio
from copy import deepcopy
from typing import Any

from langgraph.config import get_stream_writer
from pydantic import ValidationError

from app.agent.decisions import OrchestratorDecision, PlannedToolCall
from app.agent.goal_planning import planned_task
from app.agent.limits import TOOL_CONCURRENCY_LIMIT, TOOL_RETRY_POLICY
from app.agent.outcomes import tool_call_fingerprint
from app.agent.projections import apply_tool_output
from app.agent.recovery import repeats_invalidated_call
from app.agent.routing import tool_planning_subqueries
from app.agent.state import AgentState
from app.agent.tool_loop import _find_reusable_tool_result
from app.core.tracing import trace_attributes, trace_event
from app.repositories.conversations import ConversationRepository
from app.tools.schemas import ToolError, ToolExecutionResult
from app.tools.structured_planning import scoped_plan_cache


async def execute_tool_wave(
    runtime: Any,
    state: AgentState,
    *,
    repository_factory: Any = ConversationRepository,
    stream_writer_factory: Any = get_stream_writer,
) -> AgentState:
    decision = OrchestratorDecision.model_validate(state["decision"])
    repo = repository_factory(runtime.session)
    writer = stream_writer_factory()
    next_wave = state.get("tool_wave_count", 0) + 1
    used_ids = {
        str(call.get("id"))
        for wave in state.get("tool_waves", [])
        for call in wave.get("calls", [])
    } | {call.id for call in decision.tool_calls}

    for planned_call in decision.tool_calls:
        task_state = state.setdefault("task_status", {}).get(planned_call.subquery)
        if isinstance(task_state, dict):
            task_state.update(
                status="running",
                reason="tool_call_dispatched",
                wave=next_wave,
            )

    persistence_lock = asyncio.Lock()
    policy_lock = asyncio.Lock()
    concurrency = getattr(runtime, "tool_concurrency_limit", TOOL_CONCURRENCY_LIMIT)
    if not getattr(runtime.tool_executor, "supports_concurrent_calls", False):
        concurrency = 1
    # Unknown/non-read-only capabilities are not implicitly opted into concurrency.
    for call in decision.tool_calls:
        contract = runtime.contract_provider.get_contract(call.name)
        if contract is None or not contract.read_only or not (
            contract.parallel_safe or call.name == "policy_search"
        ):
            concurrency = 1
    slots = asyncio.Semaphore(concurrency)

    async def execute_call(planned_call: PlannedToolCall) -> tuple[
        PlannedToolCall, ToolExecutionResult, list[dict[str, Any]], list[dict[str, Any]], list[int],
    ]:
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        applied_memory_ids: list[int] = []
        call = planned_call
        # Observe the submitted task without adding a second validation before
        # _prepare_tool_call's existing guarded boundary.
        plan = state.get("execution_plan")
        tasks = plan.get("tasks") if isinstance(plan, dict) else None
        task_rows = tasks if isinstance(tasks, list) else []
        task = next(
            (item for item in task_rows
             if isinstance(item, dict) and item.get("id") == call.subquery),
            None,
        )
        await trace_event(
            "tool_binding_start", wave=next_wave, task_id=call.subquery,
            goal_id=task.get("goal_id") if task else None, tool_call_id=call.id,
            planned_call=planned_call, task=task,
        )
        execution: ToolExecutionResult | None = None
        reused_from_tool_call_id: str | None = None
        try:
            call, applied_memory_ids = runtime._prepare_tool_call(state, planned_call)
        except (ValidationError, ValueError) as exc:
            await trace_event(
                "tool_binding_rejected", tool_call_id=call.id,
                error_type=type(exc).__name__, reason=str(exc),
            )
            execution = ToolExecutionResult(
                tool_name=call.name,
                ok=False,
                error=ToolError(
                    code="invalid_input",
                    message=str(exc),
                    retryable=True,
                    recommended_action="replan_arguments",
                ),
            )
        else:
            await trace_event("tool_binding_ready", tool_call_id=call.id, arguments=call.arguments)
            reusable = _find_reusable_tool_result(state, call)
            if reusable is not None:
                reused_from_tool_call_id = str(reusable["tool_call_id"])
                execution = ToolExecutionResult.model_validate(reusable["execution"])
        # Freeze the bound inputs once. A retry must not rebind dependencies or memory.
        call = call.model_copy(update={"arguments": deepcopy(call.arguments)})
        contract = runtime.contract_provider.get_contract(call.name)
        if execution is None and repeats_invalidated_call(
            state, call.name, call.arguments, contract.public_input_model if contract else None,
        ):
            execution = ToolExecutionResult(tool_name=call.name, ok=False, error=ToolError(
                code="recovery_repeated_call", message="Unchanged recovery call was not executed",
            ))
        runtime_context = (
            deepcopy(_runtime_context_for_call(state, call)) if execution is None else {}
        )
        fingerprint = tool_call_fingerprint(call.name, call.arguments)
        completed_attempts = sum(
            previous.get("subquery") == call.subquery
            and previous.get("fingerprint") == fingerprint
            and previous.get("id") not in {
                result.get("tool_call_id") for result in wave.get("results", [])
                if result.get("reused_from_tool_call_id") is not None
            }
            for wave in [*state.get("tool_waves", []), {"calls": calls, "results": results}]
            for previous in wave.get("calls", [])
        )
        attempt_plans = {}
        while True:
            writer(
                {
                    "kind": "tool_call", "tool_name": call.name,
                    "status": "started", "input": call.arguments,
                }
            )
            if execution is None:
                completed_attempts += 1
                with scoped_plan_cache(attempt_plans):
                    deadline = state.get("recovery_batch", {}).get("deadline")
                    try:
                        async with asyncio.timeout_at(deadline):
                            if (deadline is not None
                                    and asyncio.get_running_loop().time() >= deadline):
                                raise TimeoutError("Recovery deadline exceeded")
                            execution = await _execute_tool_attempt(
                                runtime, call, runtime_context, task, next_wave, completed_attempts
                            )
                    except TimeoutError:
                        execution = ToolExecutionResult(
                            tool_name=call.name, ok=False, error=ToolError(
                                code="recovery_budget_exhausted",
                                message="Recovery deadline exceeded",
                            ),
                        )

            call_json = call.model_dump(mode="json")
            call_json["fingerprint"] = fingerprint
            call_json["attempt"] = completed_attempts
            execution_json = execution.model_dump(mode="json")
            await trace_event(
                "tool_result", tool_call_id=call.id, task_id=call.subquery,
                goal_id=task.get("goal_id") if task else None, tool_name=call.name,
                wave=next_wave, attempt=completed_attempts,
                execution=execution_json, reused_from_tool_call_id=reused_from_tool_call_id,
            )
            calls.append(call_json)
            result_json = {
                "tool_call_id": call.id, "name": call.name, "execution": execution_json,
            }
            if reused_from_tool_call_id is not None:
                result_json["reused_from_tool_call_id"] = reused_from_tool_call_id
            results.append(result_json)
            writer(
                {
                    "kind": "tool_call", "tool_name": call.name,
                    "status": "completed" if execution.ok else "error",
                    "input": call.arguments, "output": execution_json,
                }
            )
            if reused_from_tool_call_id is None:
                # Only the coordinator's audit session is shared; serialize its use.
                async with persistence_lock:
                    await repo.add_tool_call(
                        state["run_id"], call.name, call.arguments, execution_json,
                    )
            error = execution.error
            if (
                reused_from_tool_call_id is not None
                or execution.ok
                or error is None
                or not TOOL_RETRY_POLICY.allows_retry(
                    completed_attempts=completed_attempts, code=error.code,
                    retryable=error.retryable, action=error.recommended_action,
                )
            ):
                break
            retry_id = f"{planned_call.id}_retry_{completed_attempts}"
            while retry_id in used_ids:
                retry_id += "_retry"
            used_ids.add(retry_id)
            await trace_event(
                "tool_timeout_retry", task_id=call.subquery, wave=next_wave,
                previous_tool_call_id=call.id, tool_call_id=retry_id,
                completed_attempts=completed_attempts,
                max_attempts=TOOL_RETRY_POLICY.max_attempts,
            )
            call = call.model_copy(update={"id": retry_id})
            execution = None
        return call, execution, calls, results, applied_memory_ids

    async def dispatch(call: PlannedToolCall):
        if call.name == "policy_search":
            # A queued policy call must not occupy a general execution slot.
            async with policy_lock:
                async with slots:
                    return await execute_call(call)
        async with slots:
            return await execute_call(call)

    pending = [asyncio.create_task(dispatch(call)) for call in decision.tool_calls]
    try:
        await asyncio.gather(*pending)
    finally:
        # Join children before the caller can roll back/close its audit session.
        for job in pending:
            if not job.done():
                job.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        # Preserve completed siblings even if another call was cancelled or persistence failed.
        # Plan order, rather than completion order, determines references and projections.
        for job in pending:
            if job.cancelled() or job.exception() is not None:
                continue
            call, execution, call_rows, result_rows, memory_ids = job.result()
            calls.extend(call_rows)
            results.extend(result_rows)
            state["applied_memory_ids"] = list(dict.fromkeys([
                *state.get("applied_memory_ids", []), *memory_ids,
            ]))
            apply_tool_output(state, call, execution)
        if calls:
            wave = {"wave": next_wave, "calls": calls, "results": results}
            state.setdefault("tool_waves", []).append(wave)
            state.setdefault("tool_results", []).extend(results)
            state["tool_wave_count"] = next_wave
    return state


async def _execute_tool_attempt(
    runtime: Any, call: Any, runtime_context: dict[str, Any],
    task: dict[str, Any] | None, wave: int, attempt: int,
) -> ToolExecutionResult:
    contract = runtime.contract_provider.get_contract(call.name)
    if contract is None:
        return ToolExecutionResult(
            tool_name=call.name, ok=False,
            error=ToolError(code="unknown_tool", message=f"unknown tool: {call.name}"),
        )
    try:
        with trace_attributes(
            tool_call_id=call.id, task_id=call.subquery,
            goal_id=task.get("goal_id") if task else None, wave=wave, attempt=attempt,
        ):
            return await runtime.tool_executor.execute(
                contract, deepcopy(call.arguments), deepcopy(runtime_context),
            )
    except (ValidationError, ValueError) as exc:
        return ToolExecutionResult(
            tool_name=call.name, ok=False,
            error=ToolError(
                code="invalid_input", message=str(exc),
                retryable=True, recommended_action="replan_arguments",
            ),
        )
    except Exception as exc:  # defensive orchestration boundary; cancellation propagates
        return ToolExecutionResult(
            tool_name=call.name, ok=False,
            error=ToolError(code=type(exc).__name__, message=str(exc)),
        )


def _runtime_context_for_call(
    state: AgentState,
    call: Any,
) -> dict[str, Any]:
    context: dict[str, Any] = {"user_id": state["user_id"]}
    if call.name not in {"catalog_facets"}:
        return context
    if planned_task(state, call.subquery) is not None:
        # Formal plans already carry resolved identities in their validated arguments.
        # Do not give tools a second chance to reinterpret raw conversation memory.
        return context
    task = next(
        (
            item
            for item in tool_planning_subqueries(state.get("route_plan"))
            if item.id == str(call.subquery).strip()
        ),
        None,
    )
    context["catalog_context"] = _catalog_runtime_context(state, task)
    return context


def _catalog_runtime_context(
    state: AgentState,
    task: Any | None,
) -> dict[str, Any]:
    """Expose memory to root Tasks and direct Artifacts to dependent Tasks."""
    snapshot = state.get("working_memory_snapshot") or state.get("working_memory") or {}
    memory = snapshot.get("catalog") if isinstance(snapshot, dict) else {}
    artifacts = state.get("task_artifacts") or {}
    dependencies: list[dict[str, Any]] = []
    dependency_ids = task.depends_on if task is not None else []
    for task_id in dependency_ids:
        artifact = artifacts.get(task_id) if isinstance(artifacts, dict) else None
        if not isinstance(artifact, dict) or not artifact.get("usable"):
            continue
        value = artifact.get("value")
        value = value if isinstance(value, dict) else {}
        products = value.get("products")
        products = products if isinstance(products, list) else []
        dependencies.append(
            {
                "task_id": task_id,
                "artifact_type": str(artifact.get("artifact_type") or ""),
                "products": products,
                "selected_sku_ids": value.get("selected_sku_ids") or [],
                "selected_spu_ids": value.get("selected_spu_ids") or [],
                "selection_scope": value.get("selection_scope"),
            }
        )
    return {
        "memory": (
            memory
            if isinstance(memory, dict) and (task is None or not task.depends_on)
            else {}
        ),
        "dependency_artifacts": dependencies,
    }
