"""Business Tool wave execution and persistence."""

from typing import Any

from langgraph.config import get_stream_writer
from pydantic import ValidationError

from app.agent.decisions import OrchestratorDecision
from app.agent.outcomes import tool_call_fingerprint
from app.agent.projections import apply_tool_output
from app.agent.routing import tool_planning_subqueries
from app.agent.state import AgentState
from app.agent.tool_loop import _find_reusable_tool_result
from app.repositories.conversations import ConversationRepository
from app.tools.schemas import ToolError, ToolExecutionResult


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
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    next_wave = state.get("tool_wave_count", 0) + 1

    for planned_call in decision.tool_calls:
        task_state = state.setdefault("task_status", {}).get(planned_call.subquery)
        if isinstance(task_state, dict):
            task_state.update(
                status="running",
                reason="tool_call_dispatched",
                wave=next_wave,
            )

    for planned_call in decision.tool_calls:
        call = planned_call
        execution: ToolExecutionResult | None = None
        reused_from_tool_call_id: str | None = None
        try:
            call, applied_memory_ids = runtime._prepare_tool_call(state, planned_call)
        except ValidationError as exc:
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
            if applied_memory_ids:
                state["applied_memory_ids"] = list(
                    dict.fromkeys(
                        [*state.get("applied_memory_ids", []), *applied_memory_ids]
                    )
                )
            reusable = _find_reusable_tool_result(state, call)
            if reusable is not None:
                reused_from_tool_call_id = str(reusable["tool_call_id"])
                execution = ToolExecutionResult.model_validate(reusable["execution"])
        writer(
            {
                "kind": "tool_call",
                "tool_name": call.name,
                "status": "started",
                "input": call.arguments,
            }
        )
        if execution is None:
            contract = runtime.contract_provider.get_contract(call.name)
            if contract is None:
                execution = ToolExecutionResult(
                    tool_name=call.name,
                    ok=False,
                    error=ToolError(
                        code="unknown_tool", message=f"unknown tool: {call.name}"
                    ),
                )
            else:
                try:
                    execution = await runtime.tool_executor.execute(
                        contract,
                        call.arguments,
                        _runtime_context_for_call(state, call),
                    )
                except ValidationError as exc:
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
                except Exception as exc:  # defensive orchestration boundary
                    execution = ToolExecutionResult(
                        tool_name=call.name,
                        ok=False,
                        error=ToolError(code=type(exc).__name__, message=str(exc)),
                    )

        call_json = call.model_dump(mode="json")
        call_json["fingerprint"] = tool_call_fingerprint(call.name, call.arguments)
        execution_json = execution.model_dump(mode="json")
        calls.append(call_json)
        result_json = {
            "tool_call_id": call.id,
            "name": call.name,
            "execution": execution_json,
        }
        if reused_from_tool_call_id is not None:
            result_json["reused_from_tool_call_id"] = reused_from_tool_call_id
        results.append(result_json)
        writer(
            {
                "kind": "tool_call",
                "tool_name": call.name,
                "status": "completed" if execution.ok else "error",
                "input": call.arguments,
                "output": execution_json,
            }
        )
        if reused_from_tool_call_id is None:
            await repo.add_tool_call(
                state["run_id"],
                call.name,
                call.arguments,
                execution_json,
            )
        apply_tool_output(state, call, execution)

    wave = {
        "wave": next_wave,
        "calls": calls,
        "results": results,
    }
    state.setdefault("tool_waves", []).append(wave)
    state.setdefault("tool_results", []).extend(results)
    state["tool_wave_count"] = wave["wave"]
    return state


def _runtime_context_for_call(
    state: AgentState,
    call: Any,
) -> dict[str, Any]:
    context: dict[str, Any] = {"user_id": state["user_id"]}
    if call.name not in {"catalog_search", "catalog_compare", "catalog_facets"}:
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
                "comparison_level": value.get("comparison_level"),
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
