"""Core external-error acceptance with frozen expectations and deterministic faults."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from app.agent.legacy.error_compatibility import legacy_error_view
from app.agent.loop.query_loop import QuerySession, project_record
from app.agent.loop.tool_adapter import AdmissionError
from app.core.llm_calls import ModelCallError
from app.core.llm_feedback import OutputValidationError
from app.core.loop_budget import LoopBudget, LoopLimit
from app.schemas.catalog_search import CatalogSearchResult
from app.schemas.context import PreparedTurn
from app.tools.catalog.facets import CatalogFacetService
from app.tools.catalog.service import CatalogSearchService
from app.tools.contracts import BoundTool, ToolCatalog, default_tool_contracts, execute_bound_tool
from app.tools.orders import OrderToolService
from app.tools.output_limits import output_cost
from app.tools.schemas import OrderLookupOutput, ToolError, ToolExecutionResult
from app.tools.web_search import WebSearchError
from pydantic import ValidationError
from strands.models import Model
from strands.tools.registry import ToolRegistry

from tests.support.output_budget_fixtures import (
    catalog_output,
    document_output,
    facets_output,
    order_output,
)
from tests.support.registry_tools import adapter

CASES = json.loads(Path(__file__).with_name("cases.json").read_text())["cases"]
MAPPINGS = [case for case in CASES if case.get("fault")]
SENSITIVE = "synthetic-private-detail"
pytestmark = pytest.mark.contract


def bound(name, handler):
    return BoundTool(next(c for c in default_tool_contracts() if c.name == name), handler)


def check_error(evidence, result, code, action):
    value = result.model_dump(mode="json")
    evidence.save("execution", value)
    evidence.equal("failed_without_facts", [value["ok"], value["output"]], [False, None])
    error = value["error"]
    evidence.equal("public_classification", [error["code"], error["next_action"]], [code, action])
    evidence.equal("private_fields_absent", sorted(set(error) - {
        "code", "message", "next_action", "details",
    }), [])
    evidence.equal("private_text_absent", SENSITIVE in json.dumps(error), False)
    evidence.equal("bounded_message", 0 < len(error["message"]) <= 300, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", MAPPINGS, ids=[c["id"] for c in MAPPINGS])
async def test_final_error_mapping(case, evidence):
    calls = []

    async def handler(request):
        calls.append(request.model_dump(mode="json"))
        fault = case["fault"]
        if fault == "timeout":
            raise TimeoutError(SENSITIVE)
        if fault == "os_error":
            raise OSError(SENSITIVE)
        if fault == "unexpected":
            raise RuntimeError(SENSITIVE)
        if fault == "invalid_output":
            return {"result_type": "not_found"}
        if fault.startswith("model_"):
            raise ModelCallError(case["internal_code"], SENSITIVE,
                                 details=case.get("internal_details", {"feedback": SENSITIVE}))
        if fault.startswith("web_"):
            raise WebSearchError(case["internal_code"], SENSITIVE)
        raise AssertionError("invalid arguments/runtime/auth must not execute handler")

    result = await execute_bound_tool(bound(case["tool"], handler),
                                      case["arguments"], case["runtime"])
    check_error(evidence, result, case["code"], case["action"])
    evidence.save("handler_inputs", calls)
    evidence.equal("handler_call_count", len(calls), case["handler_calls"])
    if case["fault"] == "public_input":
        evidence.equal("safe_parameter_hint", result.error.details.model_dump(exclude_none=True), {
            "fields": [{"field": "limit", "issue": "out_of_range", "minimum": 1, "maximum": 10}],
        })
    else:
        evidence.equal("no_internal_details", result.error.details, None)


def test_E13_schema_rejects_ambiguous_or_private_errors(evidence):
    error = {"code": "internal_error", "message": "内部失败", "next_action": "stop"}
    invalid = [
        {"ok": True}, {"ok": False}, {"ok": True, "output": {}, "error": error},
        {"ok": False, "output": {}, "error": error},
        {"ok": False, "error": {**error, "code": "arbitrary_internal_code"}},
        {"ok": False, "error": {**error, "next_action": "retry"}},
        {"ok": False, "error": {**error, "retryable": True}},
        {"ok": False, "error": {**error, "details": {"stack": SENSITIVE}}},
    ]
    for value in invalid:
        with pytest.raises(ValidationError):
            ToolExecutionResult.model_validate({"tool_name": "order_lookup", **value})
    evidence.save("rejected_envelopes", invalid)
    evidence.equal("rejected_count", len(invalid), 8)
    valid = ToolExecutionResult(tool_name="order_lookup", ok=True, output={})
    evidence.equal("empty_structured_success_allowed", valid.error, None)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_E14_six_tools_through_real_registry(evidence):
    outputs = {
        "catalog_search": catalog_output(count=1),
        "catalog_lookup": catalog_output("lookup", count=1),
        "catalog_facets": facets_output(nested=1, sources=1),
        "order_lookup": order_output(count=1, padding=0, mode="single"),
        "policy_search": document_output(count=1, length=1),
        "knowledge_search": document_output("knowledge_search", count=1, length=1),
    }
    calls = []

    def handler_for(name):
        async def handler(request):
            calls.append(name)
            return outputs[name]
        return handler

    catalog = ToolCatalog([bound(name, handler_for(name)) for name in outputs])
    session = QuerySession(PreparedTurn(user_id=41, conversation_id=1, user_message_id=1,
                                       run_id=1, message="合成查询"), catalog, LoopBudget())
    agent = session.build(Mock(spec=Model))
    evidence.equal("actual_registry", isinstance(agent.tool_registry, ToolRegistry), True)
    register = agent.tool_registry.registry["register_goals"]
    registration = {"name": "register_goals", "toolUseId": "register", "input": {
        "registration": {"goals": [{"id": "g1", "source_text": "合成查询",
                                   "query": "合成查询", "disposition": "tool_planning"}]},
    }}
    events = [event async for event in register.stream(registration, {})]
    evidence.equal("goal_registered", events[-1]["tool_result"]["status"], "success")
    for name in outputs:
        arguments = {"query": "合成查询"}
        operation = session.adapter.prepare("g1", name, arguments)
        session.bindings.bind(name, operation)
        use = {"name": name, "toolUseId": name,
               "input": {"goal_id": "g1", "arguments": arguments}}
        events = [event async for event in agent.tool_registry.registry[name].stream(use, {})]
        result = events[-1]["tool_result"]
        evidence.save(name, result)
        evidence.equal(f"{name}_sdk_success", result["status"], "success")
        evidence.equal(f"{name}_no_error", result["content"][0]["json"]["result"]["error"], None)
    evidence.equal("one_call_per_tool", calls, list(outputs))


@pytest.mark.business
@pytest.mark.asyncio
async def test_E15_domain_outcomes_remain_business_results(evidence):
    scenarios = [
        ("no_match", "no_matching_sku", "explain_no_match", "completed"),
        ("clarification_required", "query_needs_clarification", "clarify_query", "not_executed"),
        ("unsupported", "request_not_supported", "explain_limitation", "not_executed"),
    ]
    for status, reason, action, query_state in scenarios:
        completed_empty = {
            "total_match_count": 0, "matched_spu_count": 0, "matched_sku_count": 0,
            "unknown_sku_count": 0, "conflicted_sku_count": 0, "missing_fact_sku_count": 0,
            "candidate_count": 0, "candidate_spu_count": 0, "candidate_sku_count": 0,
            "matching_complete": True, "is_exhaustive": True, "offset": 0, "limit": 3,
            "selection_scope": "sku", "data_quality": "clear",
        } if query_state == "completed" else {}
        output = CatalogSearchResult(result_type="empty", status=status, reason_code=reason,
                                     recommended_action=action, query_state=query_state,
                                     **completed_empty)

        async def handler(request, output=output):
            return output

        result = await execute_bound_tool(bound("catalog_search", handler),
                                          {"query": "只考虑指定型号，300元以内"}, {"user_id": 41})
        evidence.save(reason, result.model_dump(mode="json"))
        evidence.equal(f"{reason}_is_business", [result.ok, result.error], [True, None])
        evidence.equal(f"{reason}_retained", result.output["reason_code"], reason)


@pytest.mark.asyncio
async def test_E16_domain_planning_failure_is_external_error(evidence):
    class InvalidPlanner:
        calls = 0

        async def plan(self, request):
            self.calls += 1
            raise OutputValidationError(SENSITIVE)

        plan_facets = plan

    class NoDataAccess:
        def __getattr__(self, name):
            raise AssertionError(f"invalid plan must not access data: {name}")

    planner = InvalidPlanner()
    handlers = {
        "catalog_search": CatalogSearchService(NoDataAccess(), planner).search,
        "catalog_lookup": CatalogSearchService(NoDataAccess(), planner, operation="lookup").search,
        "catalog_facets": CatalogFacetService(NoDataAccess(), planner).facets,
        "order_lookup": OrderToolService(NoDataAccess(), planner=planner).lookup,
    }
    for name, handler in handlers.items():
        result = await execute_bound_tool(bound(name, handler),
                                          {"query": "比较我买过的适合办公的键盘"}, {"user_id": 41})
        check_error(evidence, result, "planning_failed", "stop")
    evidence.equal("planner_calls", planner.calls, 4)


@pytest.mark.asyncio
async def test_E17_retry_has_no_automatic_replay_and_is_bounded(evidence):
    calls = []

    async def handler(request):
        calls.append(request.model_dump(mode="json"))
        raise TimeoutError("synthetic timeout")

    query = adapter(ToolCatalog([bound("order_lookup", handler)]))
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    await query.execute(operation)
    evidence.equal("no_automatic_retry", len(calls), 1)
    await query.execute(operation)
    with pytest.raises(AdmissionError, match="retry_not_allowed"):
        await query.execute(operation)
    records = query.attempts(operation)
    evidence.save("attempts", [r.observation() for r in records])
    evidence.equal("calls_after_third_denied", len(calls), 2)
    evidence.equal("same_arguments", calls[0], calls[1])
    evidence.equal("attempt_sequence", [r.invocation.attempt for r in records], [1, 2])
    evidence.equal("same_operation", len({r.invocation.operation_id for r in records}), 1)


@pytest.mark.asyncio
async def test_E18_stop_does_not_hide_other_goal_success(evidence):
    calls = []

    async def handler(request):
        calls.append(request.order_id)
        if request.order_id == 8201:
            raise OSError(SENSITIVE)
        return OrderLookupOutput(result_type="not_found")

    query = adapter(ToolCatalog([bound("order_lookup", handler)]))
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    failure = await query.execute(operation)
    with pytest.raises(AdmissionError, match="retry_not_allowed"):
        await query.execute(operation)
    success = await query.execute(query.prepare("g2", "order_lookup", {"order_id": 8202}))
    evidence.save("handoff", [project_record(failure), project_record(success)])
    evidence.equal("attempted_orders", calls, [8201, 8202])
    evidence.equal("separate_goal_results", [failure.result.ok, success.result.ok], [False, True])
    evidence.equal("success_is_no_match_fact", success.result.output["result_type"], "not_found")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "limit", "deadline"], ids=["E19", "E20", "E21"])
async def test_control_termination_not_retryable(mode, evidence):
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def handler(request):
        started.set()
        try:
            if mode == "limit":
                raise LoopLimit("total_model_budget")
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    kwargs = {"before_attempt": lambda _: asyncio.get_running_loop().time() + 0.05}
    query = adapter(ToolCatalog([bound("order_lookup", handler)]),
                    **(kwargs if mode == "deadline" else {}))
    operation = query.prepare("g1", "order_lookup", {"order_id": 8201})
    pending = asyncio.create_task(query.execute(operation))
    await asyncio.wait_for(started.wait(), 1)
    if mode == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    elif mode == "limit":
        with pytest.raises(LoopLimit, match="total_model_budget"):
            await pending
    else:
        await pending
    evidence.equal("cleanup_completed", cleaned.is_set(), True)
    record, = query.attempts(operation)
    evidence.save("attempt", record.observation())
    check_error(evidence, record.result, "resource_limit", "stop")
    with pytest.raises(AdmissionError, match="retry_not_allowed"):
        await query.execute(operation)


@pytest.mark.asyncio
async def test_E22_output_and_handoff_budget_fail_without_false_facts(evidence):
    async def oversized(request):
        return order_output(count=1, padding=20000, mode="single")

    result = await execute_bound_tool(bound("order_lookup", oversized),
                                      {"order_id": 8201}, {"user_id": 41})
    check_error(evidence, result, "resource_limit", "narrow_request")
    evidence.equal("final_error_within_budget", output_cost(result.model_dump(mode="json"))
                   <= 12000, True)

    async def normal(request):
        return order_output(count=1, padding=1000, mode="single")

    query = adapter(ToolCatalog([bound("order_lookup", normal)]))
    record = await query.execute(query.prepare("g1", "order_lookup", {"order_id": 8201}))
    projected = project_record(record, limit=2000)
    evidence.save("original_record", record.observation())
    evidence.save("projected", projected)
    check_error(evidence, ToolExecutionResult.model_validate(projected["result"]),
                "resource_limit", "narrow_request")
    evidence.equal("source_evidence_retained", record.result.ok, True)


def test_E23_legacy_consumes_without_mutating_public_error(evidence):
    public = ToolError(
        code="dependency_unavailable", next_action="retry", message="超时",
    ).model_dump(mode="json")
    original = deepcopy(public)
    view = legacy_error_view(public)
    evidence.save("legacy_view", view)
    evidence.equal("legacy_policy_translation", [view["code"], view["recommended_action"]],
                   ["timeout", "retry_once"])
    evidence.equal("original_unchanged", public, original)
    old = {"code": "timeout", "retryable": True, "recommended_action": "retry_once"}
    evidence.equal("historical_readable", legacy_error_view(old), old)
