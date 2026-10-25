"""R04/R07 admission and lifecycle contracts, without the legacy test collection."""

import asyncio
import os
import subprocess
import sys

import pytest
from app.agent.loop.strands_tools import StrandsToolBindings
from app.agent.loop.tool_adapter import AdmissionError, QueryToolAdapter
from app.tools.contracts import (
    BoundTool,
    ToolCatalog,
    ToolRuntimeContext,
    default_tool_contracts,
)
from app.tools.schemas import OrderLookupOutput

from tests.support.registry_tools import (
    TOOL_ARGUMENTS,
    adapter,
    admit_goal,
    make_catalog,
)

pytestmark = pytest.mark.contract


def one_tool(handler, *, timeout=1):
    contract = next(c for c in default_tool_contracts() if c.llm_name == "order_lookup")
    contract = contract.model_copy(update={"timeout_seconds": timeout})
    return ToolCatalog([BoundTool(contract, handler)])


@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
def test_schema_is_derived_and_detached(monkeypatch, name):
    catalog = make_catalog(monkeypatch)
    bindings = StrandsToolBindings(adapter(catalog))
    tool = next(t for t in bindings.tools() if t.tool_name == name)
    contract = catalog.get_by_llm_name(name).contract
    schema = tool.tool_spec["inputSchema"]["json"]
    public = contract.public_input_model.model_json_schema()
    definitions = public.pop("$defs", None)
    assert schema["properties"]["arguments"] == public
    if definitions:
        assert schema["$defs"] == definitions
    assert set(schema["properties"]) == {"goal_id", "arguments"}
    assert tool.tool_spec["description"] == contract.description
    assert not {"user_id", "goal_id", "catalog_context", "evidence"} & set(
        schema["properties"]["arguments"]["properties"]
    )
    tool.tool_spec["inputSchema"]["json"]["properties"].clear()
    assert contract.public_input_model.model_json_schema()["properties"]


@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
@pytest.mark.parametrize("extra", ["user_id", "goal_id", "catalog_context", "condition_met"])
def test_public_contract_rejects_injected_runtime_or_control_fields(monkeypatch, name, extra):
    query = adapter(make_catalog(monkeypatch))
    with pytest.raises(AdmissionError, match="invalid_input"):
        query.prepare("g1", name, {**TOOL_ARGUMENTS[name], extra: 99})


@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
@pytest.mark.parametrize("limit", ["2", True, 0])
def test_wrong_types_and_ranges_are_rejected(monkeypatch, name, limit):
    query = adapter(make_catalog(monkeypatch))
    with pytest.raises(AdmissionError, match="invalid_input"):
        query.prepare("g1", name, {**TOOL_ARGUMENTS[name], "limit": limit})


@pytest.mark.asyncio
async def test_goal_guard_runs_at_prepare_and_execution(monkeypatch):
    allowed = {"g1"}

    def guard(goal_id):
        if goal_id not in allowed:
            raise AdmissionError("blocked_goal")

    query = QueryToolAdapter(
        make_catalog(monkeypatch), ToolRuntimeContext(user_id=41), admit_goal=guard,
    )
    with pytest.raises(AdmissionError, match="blocked_goal"):
        query.prepare("missing", "order_lookup", {"order_id": 8201})
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    allowed.clear()
    with pytest.raises(AdmissionError, match="blocked_goal"):
        await query.execute(operation)
    assert query.attempts(operation) == ()


@pytest.mark.parametrize("user_id", [0, -1])
def test_authentication_required(monkeypatch, user_id):
    query = QueryToolAdapter(
        make_catalog(monkeypatch), ToolRuntimeContext(user_id=user_id), admit_goal=admit_goal,
    )
    with pytest.raises(AdmissionError, match="authentication_required"):
        query.prepare("g1", "order_lookup", {"order_id": 8201})


@pytest.mark.asyncio
async def test_runtime_is_snapshot_not_model_argument():
    received = []

    async def handler(request):
        received.append(request.user_id)
        return OrderLookupOutput(result_type="not_found")

    runtime = ToolRuntimeContext(user_id=41)
    query = QueryToolAdapter(one_tool(handler), runtime, admit_goal=admit_goal)
    runtime.user_id = 99
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    exposed = query.operation(operation)
    exposed.arguments["user_id"] = 99
    result = await query.execute(operation)
    assert received == [41]
    assert "user_id" not in result.observation()["invocation"]["arguments"]


async def sdk_result(bindings, use):
    tool = next(t for t in bindings.tools() if t.tool_name == "order_lookup")
    events = [event async for event in tool.stream(use, {})]
    return events[-1]["tool_result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [
    ({"name": "policy_search"}, "bound_tool_mismatch"),
    ({"input": {"goal_id": "g1", "arguments": {"order_id": 8202}}}, "bound_arguments_mismatch"),
    ({"input": {"goal_id": "g1", "arguments": {"order_id": 8201, "user_id": 99}}},
     "invalid_input"),
])
async def test_sdk_binding_cannot_be_overridden(monkeypatch, change, code):
    query = adapter(make_catalog(monkeypatch))
    bindings = StrandsToolBindings(query)
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    bindings.bind("sdk-1", operation)
    result = await sdk_result(bindings, {
        "toolUseId": "sdk-1", "name": "order_lookup",
        "input": {"goal_id": "g1", "arguments": {"order_id": 8201}}, **change,
    })
    assert result["status"] == "error"
    assert result["content"][0]["json"]["rejection"] == code
    assert query.attempts(operation) == ()


@pytest.mark.asyncio
async def test_sdk_unbound_and_replayed_calls_fail_closed(monkeypatch):
    query = adapter(make_catalog(monkeypatch))
    bindings = StrandsToolBindings(query)
    use = {"toolUseId": "sdk-1", "name": "order_lookup",
           "input": {"goal_id": "g1", "arguments": {"order_id": 8201}}}
    assert (await sdk_result(bindings, use))["status"] == "error"
    operation = query.prepare("g1", "order_lookup", use["input"]["arguments"])
    bindings.bind("sdk-1", operation)
    assert (await sdk_result(bindings, use))["status"] == "success"
    assert (await sdk_result(bindings, use))["status"] == "error"
    with pytest.raises(AdmissionError, match="duplicate"):
        bindings.bind("sdk-1", operation)
    assert len(query.attempts(operation)) == 1


@pytest.mark.asyncio
async def test_explicit_retry_preserves_operation_and_caps_attempts():
    calls = []

    async def handler(request):
        calls.append(request)
        raise OSError("synthetic dependency down")

    query = adapter(one_tool(handler))
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    first = await query.execute(operation)
    assert len(calls) == 1  # No automatic retry.
    second = await query.execute(operation)
    assert first.invocation.operation_id == second.invocation.operation_id
    assert first.invocation.invocation_id != second.invocation.invocation_id
    assert second.invocation.attempt == 2
    with pytest.raises(AdmissionError, match="retry_not_allowed"):
        await query.execute(operation)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_success_is_not_retried(monkeypatch):
    query = adapter(make_catalog(monkeypatch))
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    await query.execute(operation)
    with pytest.raises(AdmissionError, match="retry_not_allowed"):
        await query.execute(operation)


@pytest.mark.asyncio
async def test_shared_deadline_hook_can_deny_before_execution():
    async def handler(request):
        raise AssertionError("must not execute")

    query = adapter(
        one_tool(handler), before_attempt=lambda operation: asyncio.get_running_loop().time() - 1,
    )
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    with pytest.raises(AdmissionError, match="deadline_exhausted"):
        await query.execute(operation)
    assert query.attempts(operation) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["contract_timeout", "shared_deadline", "cancel"])
async def test_hanging_tool_cleanup_and_failure_evidence(mode):
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    query = adapter(
        one_tool(handler, timeout=0.02 if mode == "contract_timeout" else 10),
        before_attempt=(lambda operation: asyncio.get_running_loop().time() + 0.02)
        if mode == "shared_deadline" else None,
    )
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    pending = asyncio.create_task(query.execute(operation))
    await asyncio.wait_for(started.wait(), 1)
    if mode == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        await pending
    assert cleaned.is_set()
    record, = query.attempts(operation)
    assert record.result.error.code == {
        "contract_timeout": "timeout", "shared_deadline": "deadline_exhausted",
        "cancel": "cancelled",
    }[mode]
    assert record.completion_blockers == ("execution_failed",)


@pytest.mark.asyncio
async def test_invalid_handler_output_is_execution_failure():
    async def handler(request):
        return {"result_type": "single_order", "order": {"id": 999}}

    query = adapter(one_tool(handler))
    record = await query.execute(query.prepare("g1", "order_lookup", {"order_id": 8201}))
    assert not record.result.ok
    assert record.result.error.details == {"failure_type": "output_contract_error"}


def test_tool_imports_do_not_load_agent_or_strands():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import app.tools.contracts; import app.tools.schemas; "
            "assert not any(n.startswith(('app.agent', 'strands')) for n in sys.modules)"
        )], env={**os.environ, "PYTHONPATH": os.environ["PYTHONPATH"]},
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_facets_uses_explicit_arguments_without_runtime_catalog_context():
    from app.tools.schemas import (
        CatalogDependencyArtifact,
        CatalogFacetOutput,
        CatalogToolRuntimeContext,
    )

    received = []

    async def handler(request):
        received.append(request.model_dump())
        return CatalogFacetOutput(result_type="empty", facet="brand")

    contract = next(c for c in default_tool_contracts() if c.llm_name == "catalog_facets")
    runtime = ToolRuntimeContext(user_id=41, catalog_context=CatalogToolRuntimeContext(
        dependency_artifacts=[CatalogDependencyArtifact(task_id="op_from_context")],
    ))
    query = QueryToolAdapter(
        ToolCatalog([BoundTool(contract, handler)]), runtime, admit_goal=admit_goal,
    )
    runtime.catalog_context.dependency_artifacts.clear()
    for goal in ("g1", "g2"):
        record = await query.execute(query.prepare(goal, "catalog_facets", {"query": "键盘品牌"}))
        assert "catalog_context" not in record.invocation.arguments
    assert contract.runtime_fields == ()
    assert all("catalog_context" not in r and r["scope"] is None for r in received)


@pytest.mark.parametrize("tool_name", ["query_complete", "record_evidence", "refund", "sql"])
def test_control_or_unknown_tools_do_not_enter_business_executor(monkeypatch, tool_name):
    query = adapter(make_catalog(monkeypatch))
    with pytest.raises(AdmissionError, match="unknown_or_non_query_tool"):
        query.prepare("g1", tool_name, {})


@pytest.mark.asyncio
async def test_concurrent_callers_are_serial_and_each_attempt_checks_budget():
    first_started, release = asyncio.Event(), asyncio.Event()
    calls, admitted = [], []

    async def handler(request):
        calls.append(request.order_id)
        if len(calls) == 1:
            first_started.set()
            await release.wait()
        return OrderLookupOutput(result_type="not_found")

    def budget(operation):
        admitted.append(operation.operation_id)
        if len(admitted) > 1:
            raise AdmissionError("tool_attempt_budget")
        return asyncio.get_running_loop().time() + 2

    query = adapter(one_tool(handler), before_attempt=budget)
    operations = [query.prepare("g1", "order_lookup", {"order_id": i}) for i in (8201, 8202)]
    first = asyncio.create_task(query.execute(operations[0]))
    await asyncio.wait_for(first_started.wait(), 1)
    second = asyncio.create_task(query.execute(operations[1]))
    await asyncio.sleep(0)
    assert calls == [8201] and len(admitted) == 1
    release.set()
    await first
    with pytest.raises(AdmissionError, match="tool_attempt_budget"):
        await second
    assert calls == [8201] and admitted == operations
    assert query.attempts(operations[1]) == ()
