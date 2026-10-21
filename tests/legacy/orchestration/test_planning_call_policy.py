import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest
from app.agent.legacy.goal_planning import (
    PlanningCallError,
    _transport_retry_delay,
    plan_tools,
    route_goals,
)
from app.agent.legacy.limits import PLANNING_CALL_POLICY, PlanningCallPolicy
from app.agent.legacy.runtime import AgentRuntime
from app.core.config import Settings
from app.schemas.chat import ChatRequest
from app.tools.contracts import DefaultToolContractProvider
from openai import APIConnectionError, APITimeoutError
from pydantic import ValidationError

from tests.support.orchestration_planning_call_policy import (
    SequenceModel,
    generate,
    http_error,
    policy,
)
from tests.support.runtime import FakeContext, goal, message, setup_runtime


def test_central_defaults_and_immutability():
    assert PLANNING_CALL_POLICY == PlanningCallPolicy(30, 90, 1, 1, 3, 1, 0)
    with pytest.raises(FrozenInstanceError):
        PLANNING_CALL_POLICY.max_attempts = 10


@pytest.mark.parametrize(
    "changes",
    [
        {"call_timeout_seconds": 0},
        {"total_timeout_seconds": float("inf")},
        {"retry_delay_seconds": -1},
        {"max_attempts": 0},
        {"max_output_repairs": -1},
        {"max_transport_retries": 1.5},
        {"sdk_max_retries": 1},
    ],
)
def test_invalid_policy_rejected(changes):
    with pytest.raises(ValueError):
        replace(PLANNING_CALL_POLICY, **changes)


def test_runtime_supplies_policy_to_both_planners_only(monkeypatch):
    builds = []

    class Bindable:
        def bind_tools(self, *args, **kwargs):
            return self

    def build(settings, **kwargs):
        builds.append(kwargs)
        return Bindable()

    monkeypatch.setattr("app.agent.legacy.runtime.build_chat_model", build)
    AgentRuntime(
        None,
        Settings(llm_api_key=""),
        context_service=FakeContext(),
        tool_executor=object(),
        planning_call_policy=policy(call_timeout_seconds=17),
    )
    assert builds[0] == {"temperature": 0.2, "max_retries": 0, "timeout": 30}
    assert builds[1:] == [{"temperature": 0.0, "max_retries": 0, "timeout": 17}] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        APITimeoutError(request=httpx.Request("POST", "https://model.invalid")),
        APIConnectionError(request=httpx.Request("POST", "https://model.invalid")),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("offline"),
        *[http_error(code) for code in (408, 409, 429, 500, 502, 503, 504)],
    ],
)
async def test_transient_failures_retry_same_payload_once(error):
    model = SequenceModel(error, message("plan", {"value": 1}))
    assert (await generate(model)).value == 1
    assert len(model.calls) == 2
    assert model.calls[0] == model.calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_http_errors_stop_immediately_without_repair(status):
    model = SequenceModel(http_error(status))
    with pytest.raises(PlanningCallError) as caught:
        await generate(model)
    assert caught.value.code == "planning_model_rejected"
    assert "private provider response" not in str(caught.value)
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("bug"), TypeError("bug"), OSError("unknown")])
async def test_unclassified_program_errors_are_not_retried(error):
    model = SequenceModel(error)
    with pytest.raises(type(error)) as caught:
        await generate(model)
    assert caught.value is error and len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "errors",
    [
        [http_error(503), message("plan", {"value": -1})],
        [message("plan", {"value": -1}), http_error(503)],
    ],
)
async def test_transport_and_repair_have_separate_counters_but_shared_attempt_cap(errors):
    model = SequenceModel(*errors, message("plan", {"value": 1}))
    assert (await generate(model)).value == 1
    assert len(model.calls) == 3
    feedback = json.loads(model.calls[2][-1].content)
    assert "validation_feedback" in feedback
    assert "private provider response" not in model.calls[2][-1].content


@pytest.mark.asyncio
async def test_repeated_transport_failure_does_not_spend_output_repair_slot():
    model = SequenceModel(http_error(503), http_error(503))
    with pytest.raises(PlanningCallError) as caught:
        await generate(model)
    assert caught.value.code == "planning_transport_failed" and len(model.calls) == 2


@pytest.mark.asyncio
async def test_repeated_bad_output_is_only_repaired_once():
    model = SequenceModel(message("plan", {"value": 0}), message("plan", {"value": 0}))
    with pytest.raises(ValidationError):
        await generate(model)
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_disabled_transport_retries_do_not_use_a_repair_slot():
    model = SequenceModel(http_error(503))
    with pytest.raises(PlanningCallError):
        await generate(model, policy(max_transport_retries=0))
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_attempt_cap_wins_even_if_retry_counters_have_room():
    model = SequenceModel(http_error(503), message("plan", {"value": 0}), http_error(503))
    with pytest.raises(PlanningCallError):
        await generate(model, policy(max_transport_retries=5, max_output_repairs=5))
    assert len(model.calls) == 3


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"retry-after": "2"}, 2),
        ({"retry-after-ms": "250"}, 0.25),
        ({"retry-after": "invalid"}, 1),
        ({"retry-after": "-2"}, 1),
        ({"retry-after": "nan"}, 1),
        ({"retry-after": "inf"}, 1),
        ({"retry-after-ms": "bad", "retry-after": "2"}, 2),
    ],
)
def test_retry_after_numeric_and_invalid_values(headers, expected):
    assert _transport_retry_delay(http_error(429, headers), 1) == expected


def test_retry_after_http_date(monkeypatch):
    monkeypatch.setattr("app.core.llm_calls.time.time", lambda: 1000)
    future = format_datetime(datetime.fromtimestamp(1007, UTC), usegmt=True)
    assert _transport_retry_delay(http_error(429, {"retry-after": future}), 1) == 7


@pytest.mark.asyncio
async def test_retry_after_that_exceeds_budget_stops_without_sleep(monkeypatch):
    async def forbidden(*args):
        pytest.fail("Must not sleep or retry past the total budget")

    monkeypatch.setattr("app.agent.legacy.goal_planning.asyncio.sleep", forbidden)
    model = SequenceModel(http_error(429, {"retry-after": "120"}))
    with pytest.raises(PlanningCallError) as caught:
        await generate(model)
    assert caught.value.code == "planning_deadline_exceeded" and len(model.calls) == 1


@pytest.mark.asyncio
async def test_valid_retry_after_is_used_instead_of_default_delay(monkeypatch):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("app.agent.legacy.goal_planning.asyncio.sleep", sleep)
    model = SequenceModel(http_error(429, {"retry-after": "2"}), message("plan", {"value": 1}))
    await generate(model)
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_hanging_call_is_cancelled_and_retried_at_most_once():
    cancelled = []

    async def hang():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    model = SequenceModel(hang, hang)
    with pytest.raises(PlanningCallError) as caught:
        await asyncio.wait_for(generate(model, policy(call_timeout_seconds=0.01)), 1)
    assert caught.value.code == "planning_call_timeout"
    assert len(model.calls) == len(cancelled) == 2


@pytest.mark.asyncio
async def test_global_deadline_stops_hang_without_using_retry_slot():
    async def hang():
        await asyncio.Event().wait()

    model = SequenceModel(hang)
    with pytest.raises(PlanningCallError) as caught:
        await asyncio.wait_for(generate(model, policy(total_timeout_seconds=0.02)), 1)
    assert caught.value.code == "planning_deadline_exceeded" and len(model.calls) == 1


@pytest.mark.asyncio
async def test_late_result_after_suppressed_cancel_is_not_accepted():
    async def late():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return message("plan", {"value": 1})

    model = SequenceModel(late, late)
    with pytest.raises(PlanningCallError) as caught:
        await asyncio.wait_for(generate(model, policy(call_timeout_seconds=0.01)), 1)
    assert caught.value.code == "planning_call_timeout" and len(model.calls) == 2


@pytest.mark.asyncio
async def test_shared_budget_covers_both_live_stages():
    text = "查询键盘"

    async def first():
        await asyncio.sleep(0.01)
        return message("route_request", {"rewritten_query": text, "goals": [goal(text)]})

    async def second():
        await asyncio.Event().wait()

    runtime = SimpleNamespace(
        request_router=SequenceModel(first),
        task_planner=SequenceModel(second),
        contract_provider=DefaultToolContractProvider(),
        planning_call_policy=policy(total_timeout_seconds=0.1),
    )
    state = {"message": text}
    route = await route_goals(runtime, state)
    deadline = state["planning_deadline_monotonic"]
    state["route_plan"] = route.model_dump(mode="json")
    with pytest.raises(PlanningCallError) as caught:
        await asyncio.wait_for(plan_tools(runtime, state), 1)
    assert caught.value.code == "planning_deadline_exceeded"
    assert state["planning_deadline_monotonic"] == deadline
    assert state["request_router_call_count"] == state["task_planner_call_count"] == 1


@pytest.mark.asyncio
async def test_deadline_also_bounds_retry_wait(monkeypatch):
    original_sleep = asyncio.sleep

    async def stalled_sleep(delay):
        await original_sleep(10)

    monkeypatch.setattr("app.agent.legacy.goal_planning.asyncio.sleep", stalled_sleep)
    model = SequenceModel(http_error(503))
    with pytest.raises(PlanningCallError) as caught:
        await asyncio.wait_for(generate(model, policy(total_timeout_seconds=0.02)), 1)
    assert caught.value.code == "planning_deadline_exceeded" and len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("during_backoff", [False, True])
async def test_external_cancellation_propagates_without_retry_or_repair(during_backoff):
    entered = asyncio.Event()

    async def call():
        entered.set()
        if during_backoff:
            raise http_error(503)
        await asyncio.Event().wait()

    model = SequenceModel(call)
    pending = asyncio.create_task(generate(model, PLANNING_CALL_POLICY))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_llm2_inherits_llm1_deadline_and_cannot_call_after_it_expires():
    text = "查询键盘"
    router = SequenceModel(
        message("route_request", {"rewritten_query": text, "goals": [goal(text)]})
    )
    planner = SequenceModel()
    runtime = SimpleNamespace(
        request_router=router,
        task_planner=planner,
        contract_provider=DefaultToolContractProvider(),
        planning_call_policy=policy(),
    )
    state = {"message": text}
    route = await route_goals(runtime, state)
    assert state["request_router_call_count"] == 1
    state["route_plan"] = route.model_dump(mode="json")
    expired = asyncio.get_running_loop().time() - 1
    state["planning_deadline_monotonic"] = expired
    with pytest.raises(PlanningCallError) as caught:
        await plan_tools(runtime, state)
    assert caught.value.code == "planning_deadline_exceeded"
    assert state["planning_deadline_monotonic"] == expired
    assert not planner.calls and "execution_plan" not in state


@pytest.mark.asyncio
async def test_router_counts_network_retry_and_output_repair_as_real_attempts():
    text = "查询键盘"
    router = SequenceModel(
        http_error(503),
        message("route_request", {}),
        message("route_request", {"rewritten_query": text, "goals": [goal(text)]}),
    )
    runtime = SimpleNamespace(request_router=router, planning_call_policy=policy())
    state = {"message": text}
    await route_goals(runtime, state)
    assert state["request_router_call_count"] == len(router.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["llm1", "llm2"])
async def test_production_graph_timeout_returns_controlled_failure_without_tools_or_llm3(
    monkeypatch, stage,
):
    text = "查询键盘"
    runtime, model, executor, context = setup_runtime(monkeypatch, text, [goal(text)], [])
    runtime.planning_call_policy = policy(call_timeout_seconds=0.01)
    saved = []
    complete = context.complete_turn

    async def capture(prepared, outcome):
        saved.append(outcome)
        return await complete(prepared, outcome)

    context.complete_turn = capture

    async def hang():
        await asyncio.Event().wait()

    failing = SequenceModel(hang, hang)
    if stage == "llm1":
        runtime.request_router = failing
    else:
        runtime.task_planner = failing
    request = ChatRequest(message=text)
    result = await asyncio.wait_for(
        runtime._build_graph().ainvoke(runtime._initial_state(request, 1)), 1
    )
    assert result["planning_error"]["errors"][0]["code"] == "planning_call_timeout"
    assert (
        result["request_router_call_count" if stage == "llm1" else "task_planner_call_count"]
        == 2
    )
    answer = result["answer"]
    assert "未能完成" in answer and "规划" in answer
    assert context.completed == 1 and not executor.calls
    assert "planning_deadline_monotonic" not in saved[0]
    assert saved[0]["planning_error"]["errors"][0]["code"] == "planning_call_timeout"
    assert len(failing.calls) == 2
    assert not model.calls.get("summarize_goals")
