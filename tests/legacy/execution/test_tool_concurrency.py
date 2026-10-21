import asyncio

import pytest
from app.core.config import Settings
from app.tools.contracts import RegistryToolExecutor
from app.tools.schemas import OrderLookupOutput

from tests.support.execution_tool_concurrency import (
    SessionFactory,
    success,
    wave_fixture,
)


@pytest.mark.asyncio
async def test_concurrency_is_bounded_and_results_keep_plan_order():
    started, finished = [], []
    gates = [asyncio.Event() for _ in range(4)]
    first_three, fourth = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def execute(contract, arguments, trusted):
        nonlocal active, peak
        i = int(arguments["query"])
        assert trusted["user_id"] == 7
        started.append(i)
        active += 1
        peak = max(peak, active)
        if len(started) == 3:
            first_three.set()
        if i == 3:
            fourth.set()
        try:
            await gates[i].wait()
            finished.append(i)
            return success()
        finally:
            active -= 1

    run, state, _, persisted, events = wave_fixture(execute)
    job = asyncio.create_task(run())
    try:
        await asyncio.wait_for(first_three.wait(), 1)
        assert started == [0, 1, 2] and not fourth.is_set()
        gates[2].set()
        await asyncio.wait_for(fourth.wait(), 1)
        for gate in gates:
            gate.set()
        await asyncio.wait_for(job, 1)
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
    assert peak == 3 and active == 0 and finished[0] == 2
    assert [r["tool_call_id"] for r in state["tool_results"]] == [f"call_{i}" for i in range(4)]
    assert state["applied_memory_ids"] == [0, 1, 2, 3]
    assert len(persisted) == 4 and len(events) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("serial_reason", ["configured", "legacy_executor", "unsafe_contract"])
async def test_serial_mode_and_unopted_executors_remain_serial(serial_reason):
    started = []
    active = peak = 0

    async def execute(contract, arguments, trusted):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(arguments["query"])
        await asyncio.sleep(0)
        active -= 1
        return success()

    run, _, runtime, _, _ = wave_fixture(execute, limit=1 if serial_reason == "configured" else 3)
    if serial_reason == "legacy_executor":
        del runtime.tool_executor.supports_concurrent_calls
    elif serial_reason == "unsafe_contract":
        original = runtime.contract_provider.get_contract
        runtime.contract_provider.get_contract = lambda name: original(name).model_copy(
            update={"read_only": False},
        )
    await run()
    assert peak == 1 and started == ["0", "1", "2", "3"]


@pytest.mark.asyncio
async def test_policy_calls_serialize_without_blocking_other_tools():
    started = []
    policy_started, other_started, release = (asyncio.Event() for _ in range(3))

    async def execute(contract, arguments, trusted):
        i = int(arguments["query"])
        started.append(i)
        if i == 0:
            policy_started.set()
            await release.wait()
        elif i == 2:
            other_started.set()
        return success(contract.name)

    run, _, _, _, _ = wave_fixture(
        execute, ["policy_search", "policy_search", "order_lookup"], limit=2,
    )
    job = asyncio.create_task(run())
    try:
        await asyncio.wait_for(policy_started.wait(), 1)
        await asyncio.wait_for(other_started.wait(), 1)
        assert started == [0, 2]
        release.set()
        await asyncio.wait_for(job, 1)
        assert started == [0, 2, 1]
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_joins_active_tasks_and_does_not_start_queued_tasks():
    started, closed = [], []
    ready = asyncio.Event()

    async def execute(contract, arguments, trusted):
        i = arguments["query"]
        started.append(i)
        if len(started) == 3:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(i)

    run, _, _, persisted, _ = wave_fixture(execute)
    job = asyncio.create_task(run())
    await asyncio.wait_for(ready.wait(), 1)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert set(started) == set(closed) == {"0", "1", "2"}
    assert persisted == []


@pytest.mark.asyncio
async def test_timeout_retry_keeps_per_task_cache_and_independent_success():
    from app.tools.schemas import ToolError, ToolExecutionResult
    from app.tools.structured_planning import _attempt_plans

    both_started = asyncio.Event()
    attempts, caches = {}, {}

    async def execute(contract, arguments, trusted):
        i = arguments["query"]
        attempts[i] = attempts.get(i, 0) + 1
        cache = _attempt_plans.get()
        if i not in caches:
            caches[i] = cache
            cache["owner"] = i
        assert cache is caches[i] and cache["owner"] == i
        if len(attempts) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        if i == "0":
            return ToolExecutionResult(tool_name=contract.name, ok=False, error=ToolError(
                code="timeout", message="synthetic timeout", retryable=True,
                recommended_action="retry_once",
            ))
        return success()

    run, state, _, persisted, _ = wave_fixture(execute, ["order_lookup"] * 2)
    await run()
    assert attempts == {"0": 2, "1": 1} and caches["0"] is not caches["1"]
    assert [r["execution"]["ok"] for r in state["tool_results"]] == [False, False, True]
    assert len(persisted) == 3 and _attempt_plans.get() is None


@pytest.mark.asyncio
async def test_registry_executes_with_distinct_live_sessions(monkeypatch):
    factory = SessionFactory()
    both_started = asyncio.Event()
    seen = []

    async def lookup(self, request):
        seen.append(self.session)
        if len(seen) == 2:
            assert len(factory.opened) == 2 and not factory.closed
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        assert self.session not in factory.closed
        return OrderLookupOutput(result_type="not_found")

    monkeypatch.setattr("app.tools.orders.OrderToolService.lookup", lookup)
    executor = RegistryToolExecutor(
        object(), Settings(_env_file=None, llm_api_key=""), tool_session_factory=factory,
    )
    run, state, runtime, _, _ = wave_fixture(executor.execute, ["order_lookup"] * 2)
    runtime.tool_executor = executor
    await run()
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert set(factory.closed) == set(seen)
    assert all(row["execution"]["ok"] for row in state["tool_results"])


@pytest.mark.asyncio
async def test_cancel_preserves_completed_sibling():
    hanging, saved = asyncio.Event(), asyncio.Event()

    async def execute(contract, arguments, trusted):
        if arguments["query"] == "0":
            hanging.set()
            await asyncio.Event().wait()
        return success()

    run, state, _, persisted, events = wave_fixture(execute, ["order_lookup"] * 2)
    job = asyncio.create_task(run())
    try:
        await asyncio.wait_for(hanging.wait(), 1)
        # The audit fake yields before appending; wait for its completed write.
        async def wait_saved():
            while not persisted:
                await asyncio.sleep(0)
            saved.set()
        await asyncio.wait_for(wait_saved(), 1)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert saved.is_set()
        assert [r["tool_call_id"] for r in state["tool_results"]] == ["call_1"]
        assert len(persisted) == 1
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_formal_graph_overlaps_roots_then_binds_dependent_task(monkeypatch, tmp_path):
    import json

    from app.core.tracing import agent_trace, trace_event
    from app.schemas.chat import ChatRequest
    from app.tools.schemas import ToolExecutionResult

    from tests.support.runtime import goal, setup_runtime, task

    first, second, dependent = "查白色键盘", "查黑色键盘", "第一款多少钱"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch, first + second,
        [goal(first), goal(second, identity="goal_2")],
        [{"goal_id": "goal_1", "tasks": [
            task(first, answer_role="internal"),
            task(dependent, "task_3", depends_on=["task_1"], bindings=[{
                "source": "task_output", "ref": "task_1", "kind": "product",
                "positions": [1], "selection": "candidate_versions",
            }]),
        ]}, {"goal_id": "goal_2", "tasks": [task(second, "task_2")]}],
    )
    roots, completed = [], []
    ready = asyncio.Event()
    bound = []

    async def execute(contract, arguments, trusted):
        query = arguments["query"]
        if query != dependent:
            roots.append(query)
            if len(roots) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 1)
        else:
            assert set(completed) == {first, second}
            bound.append(arguments)
        await trace_event("concurrency_test_tool", query=query)
        completed.append(query)
        product = executor.search.repository.rows[0].product.model_dump(mode="json")
        return ToolExecutionResult(tool_name=contract.name, ok=True, output={
            "result_type": "products", "products": [product], "query_plan": {},
        })

    executor.execute = execute
    settings = Settings(_env_file=None, llm_api_key="", agent_trace_mode="local",
                        agent_trace_dir=tmp_path)
    with agent_trace(settings) as recording:
        state = await runtime._build_graph().ainvoke(
            runtime._initial_state(ChatRequest(message=first + second), 1),
        )
    assert state["tool_wave_count"] == 2
    assert all(item["status"] == "succeeded" for item in state["task_status"].values())
    assert bound[0]["scope"]["sku_ids"] == [1]
    rows = [
        json.loads(line)
        for line in (recording.directory / "trace.jsonl").read_text().splitlines()
    ]
    probes = [row for row in rows if row.get("name") == "concurrency_test_tool"]
    assert len(probes) == 3
    assert {row["data"]["task_id"] for row in probes} == {"task_1", "task_2", "task_3"}


@pytest.mark.asyncio
async def test_audit_failure_cancels_and_joins_sibling_before_propagating():
    from app.agent.legacy.execution import execute_tool_wave

    running, closed = asyncio.Event(), asyncio.Event()

    async def execute(contract, arguments, trusted):
        if arguments["query"] == "0":
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        await running.wait()
        return success()

    _, state, runtime, _, _ = wave_fixture(execute, ["order_lookup"] * 2)

    class BrokenAudit:
        def __init__(self, session):
            pass

        async def add_tool_call(self, *args):
            raise RuntimeError("synthetic audit failure")

    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await execute_tool_wave(
            runtime, state, repository_factory=BrokenAudit,
            stream_writer_factory=lambda: lambda _: None,
        )
    assert closed.is_set()
