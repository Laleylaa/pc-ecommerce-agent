"""Bounded tool timeout recovery through the production graph, without external I/O."""

import asyncio
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
from app.agent.legacy.execution import execute_tool_wave
from app.agent.legacy.limits import TOOL_RETRY_POLICY, ToolRetryPolicy
from app.agent.legacy.tool_loop import _deterministic_recovery_decision
from app.core.config import Settings
from app.core.tracing import agent_trace
from app.schemas.chat import ChatRequest
from app.tools.contracts import BoundTool, execute_bound_tool
from app.tools.schemas import ToolError, ToolExecutionResult

from tests.support.runtime import goal, keyboard_plan, setup_runtime, task


def failure(code="timeout", *, retryable=True, action="retry_once"):
    return ToolExecutionResult(
        tool_name="catalog_search", ok=False,
        error=ToolError(
            code=code, message="isolated tool failure",
            retryable=retryable, recommended_action=action,
        ),
    )


def single(monkeypatch):
    query = "白色蓝牙键盘300元以内"
    return query, setup_runtime(
        monkeypatch, query, [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query)]}], [keyboard_plan()],
    )


async def invoke(runtime, query):
    return await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=query), 1)
    )


def test_policy_is_central_and_immutable():
    assert TOOL_RETRY_POLICY.max_timeout_retries == 1
    assert TOOL_RETRY_POLICY.max_attempts == 2
    with pytest.raises(FrozenInstanceError):
        TOOL_RETRY_POLICY.max_timeout_retries = 3


@pytest.mark.parametrize("count", [-1, 2, True, 1.0])
def test_retry_once_policy_rejects_invalid_counts(count):
    with pytest.raises(ValueError):
        ToolRetryPolicy(max_timeout_retries=count)


@pytest.mark.asyncio
async def test_timeout_success_preserves_inputs_and_records_both_attempts(monkeypatch, tmp_path):
    query, (runtime, model, executor, context) = single(monkeypatch)
    original = executor.execute
    attempts, persisted, streamed = [], [], []

    async def execute(contract, arguments, runtime_context):
        attempts.append(deepcopy((contract.name, arguments, runtime_context)))
        if len(attempts) == 1:
            # A faulty executor must not mutate the retry's frozen inputs.
            arguments["query"] = "incorrect broadened query"
            runtime_context["user_id"] = 999
            return failure()
        return await original(contract, arguments, runtime_context)

    class Audit:
        def __init__(self, session):
            assert session is None

        async def add_tool_call(self, *args):
            persisted.append(deepcopy(args))

    executor.execute = execute
    monkeypatch.setattr("app.agent.legacy.runtime.ConversationRepository", Audit)
    monkeypatch.setattr("app.agent.legacy.runtime.get_stream_writer", lambda: streamed.append)
    settings = Settings(
        _env_file=None, llm_api_key="", agent_trace_mode="local", agent_trace_dir=tmp_path,
    )
    with agent_trace(settings) as recording:
        state = await invoke(runtime, query)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert attempts[1][2]["user_id"] == 1
    assert attempts[1][1]["query"] == query
    assert state["tool_wave_count"] == 1
    calls = state["tool_waves"][0]["calls"]
    assert len({call["id"] for call in calls}) == 2
    assert [call["attempt"] for call in calls] == [1, 2]
    assert len(persisted) == 2
    assert [event["status"] for event in streamed] == ["started", "error", "started", "completed"]
    assert state["task_status"]["task_1"]["status"] == "succeeded"
    assert state["task_artifacts"]["task_1"]["source_tool_call_id"] == calls[-1]["id"]
    assert state["subquery_ledger"][0]["status"] == "superseded"
    assert state["subquery_ledger"][-1]["outcome"] == "usable"
    assert len(model.calls["route_request"]) == len(model.calls["submit_execution_plan"]) == 1
    assert context.completed == 1
    rows = [
        json.loads(line)
        for line in (recording.directory / "trace.jsonl").read_text().splitlines()
    ]
    retries = [row for row in rows if row.get("name") == "tool_timeout_retry"]
    assert len(retries) == 1 and retries[0]["data"]["max_attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [
        failure(action="stop"), failure(retryable=False),
        failure("invalid_input", action="replan_arguments"),
        failure("dependency_unavailable", action="explain_temporary_unavailability"),
        failure("unauthorized", action="request_authentication"),
        failure("execution_error", action="stop"),
        failure("execution_error"),
    ],
)
async def test_nonretryable_outcomes_execute_once(monkeypatch, error):
    query, (runtime, _, executor, _) = single(monkeypatch)
    attempts = []

    async def execute(*args):
        attempts.append(args)
        return error

    executor.execute = execute
    state = await invoke(runtime, query)
    assert len(attempts) == 1
    assert state["task_status"]["task_1"]["status"] == "failed"
    assert _deterministic_recovery_decision(state) is None


@pytest.mark.asyncio
async def test_retry_can_be_disabled_in_shared_policy(monkeypatch):
    disabled = ToolRetryPolicy(max_timeout_retries=0)
    monkeypatch.setattr("app.agent.legacy.execution.TOOL_RETRY_POLICY", disabled)
    monkeypatch.setattr("app.agent.legacy.tool_loop.TOOL_RETRY_POLICY", disabled)
    query, (runtime, _, executor, _) = single(monkeypatch)
    attempts = []

    async def execute(*args):
        attempts.append(args)
        return failure()

    executor.execute = execute
    state = await invoke(runtime, query)
    assert len(attempts) == 1
    assert _deterministic_recovery_decision(state) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out_task", ["root", "dependent", "both", "root_exhausted"])
async def test_timeout_attempts_do_not_consume_dependency_waves(monkeypatch, timed_out_task):
    query, downstream = "白色蓝牙键盘300元以内", "查询第一款当前价格"
    runtime, model, executor, _ = setup_runtime(
        monkeypatch, query, [goal(query)],
        [{"goal_id": "goal_1", "tasks": [
            task(query, answer_role="internal"),
            task(downstream, "task_2", depends_on=["task_1"], bindings=[{
                "source": "task_output", "ref": "task_1", "kind": "product",
                "positions": [1], "selection": "candidate_versions",
            }]),
        ]}],
        [keyboard_plan(), {"operation": "lookup", "purpose": "lookup"}],
    )
    original = executor.execute
    attempts = {query: [], downstream: []}

    async def execute(contract, arguments, runtime_context):
        seen = attempts[arguments["query"]]
        seen.append(deepcopy((arguments, runtime_context)))
        should_fail = (
            timed_out_task in {"both", "root_exhausted"}
            or (timed_out_task == "root" and arguments["query"] == query)
            or (timed_out_task == "dependent" and arguments["query"] == downstream)
        )
        if should_fail and (len(seen) == 1 or timed_out_task == "root_exhausted"):
            return failure()
        return await original(contract, arguments, runtime_context)

    executor.execute = execute
    state = await invoke(runtime, query)
    assert len(model.calls["submit_execution_plan"]) == 1
    if timed_out_task == "root_exhausted":
        assert len(attempts[query]) == 2 and attempts[downstream] == []
        assert state["tool_wave_count"] == 1
        assert state["task_status"]["task_1"]["status"] == "failed"
        assert state["task_status"]["task_2"]["status"] == "blocked"
    else:
        assert state["tool_wave_count"] == 2
        assert all(value["status"] == "succeeded" for value in state["task_status"].values())
        assert attempts[downstream][0][0]["scope"]["sku_ids"] == [1]
        for seen in attempts.values():
            assert len(seen) in (1, 2)
            if len(seen) == 2:
                assert seen[0] == seen[1]
        expected_attempts = 4 if timed_out_task == "both" else 3
        assert sum(len(seen) for seen in attempts.values()) == expected_attempts


@pytest.mark.asyncio
async def test_exhausted_task_does_not_affect_independent_task(monkeypatch):
    first, second = "查询白色键盘", "查询蓝牙键盘"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch, first + "；" + second,
        [goal(first), goal(second, identity="goal_2")],
        [{"goal_id": "goal_1", "tasks": [task(first)]},
         {"goal_id": "goal_2", "tasks": [task(second, "task_2")]}],
        [{"category": "键盘", "category_evidence": "键盘"}],
    )
    original = executor.execute
    attempts = []

    async def execute(contract, arguments, runtime_context):
        attempts.append(arguments["query"])
        if arguments["query"] == first:
            return failure()
        return await original(contract, arguments, runtime_context)

    executor.execute = execute
    state = await invoke(runtime, first + "；" + second)
    assert attempts == [first, first, second]
    assert state["tool_wave_count"] == 1
    assert state["task_status"]["task_1"]["status"] == "failed"
    assert state["task_status"]["task_2"]["status"] == "succeeded"
    assert state["decision"]["control_action"] == "finish_partial"


@pytest.mark.asyncio
async def test_actual_bound_tool_timeout_is_cancelled_then_retried(monkeypatch):
    query, (runtime, _, executor, _) = single(monkeypatch)
    inputs, cancelled = [], []

    async def handler(request):
        inputs.append(request.model_dump(mode="json"))
        if len(inputs) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        return await executor.search.search(request)

    async def execute(contract, arguments, runtime_context):
        bounded = contract.model_copy(update={"timeout_seconds": .05})
        return await execute_bound_tool(BoundTool(bounded, handler), arguments, runtime_context)

    executor.execute = execute
    state = await invoke(runtime, query)
    assert cancelled == [True] and len(inputs) == 2 and inputs[0] == inputs[1]
    assert state["task_status"]["task_1"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_external_cancellation_propagates_without_retry(monkeypatch):
    query, (runtime, _, executor, _) = single(monkeypatch)
    started, attempts = asyncio.Event(), []

    async def execute(*args):
        attempts.append(args)
        started.set()
        await asyncio.Event().wait()

    executor.execute = execute
    run = asyncio.create_task(invoke(runtime, query))
    await asyncio.wait_for(started.wait(), 2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_exhausted_call_reuse_does_not_restart_timeout_budget(monkeypatch):
    query, (runtime, _, executor, _) = single(monkeypatch)
    attempts = []

    async def execute(*args):
        attempts.append(args)
        return failure()

    executor.execute = execute
    state = await invoke(runtime, query)
    state["decision"] = {
        "type": "tool_calls", "tool_calls": [{
            **state["tool_waves"][0]["calls"][0], "id": "extra_call",
        }],
    }
    # Runtime call models prohibit diagnostic fields stored only in wave records.
    state["decision"]["tool_calls"][0].pop("fingerprint")
    state["decision"]["tool_calls"][0].pop("attempt")

    class Audit:
        def __init__(self, session):
            assert session is None

        async def add_tool_call(self, *args):
            pytest.fail("Reusing an exhausted result must not persist another execution")

    await execute_tool_wave(
        runtime, state, repository_factory=Audit, stream_writer_factory=lambda: lambda _: None,
    )
    assert len(attempts) == 2
    assert state["tool_waves"][-1]["results"][0]["reused_from_tool_call_id"]
