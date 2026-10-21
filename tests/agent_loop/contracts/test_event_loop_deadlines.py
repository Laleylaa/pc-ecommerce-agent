"""Different clock origins must preserve real expiry, HTTP limits and cleanup."""

import asyncio

import httpx
import pytest
from agent_loop.support.domain_tools import adapter
from agent_loop.support.event_loops import LOOP_KINDS, new_loop
from agent_loop.support.loop_runtime import (
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from app.agent.loop.tool_adapter import AdmissionError
from app.core.loop_budget import LoopBudget, LoopLimit, model_stage
from app.core.loop_models import LoopUsage
from app.schemas.chat import ChatRequest
from app.tools.contracts import BoundTool, ToolCatalog, default_tool_contracts

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("loop_kind", LOOP_KINDS)
def test_budget_and_http_timeout_use_same_clock(loop_kind):
    async def scenario():
        loop = asyncio.get_running_loop()
        before = loop.time()
        budget = LoopBudget(query_seconds=2, answer_seconds=0.02)
        assert before <= budget.started <= loop.time()
        assert 1 < budget.query_deadline - loop.time() <= 2
        usage = LoopUsage(budget)
        request = httpx.Request("POST", "https://fixture.invalid")
        await usage.on_request(request)
        assert all(1 < value <= 2 for value in request.extensions["timeout"].values())
        budget.started -= 3
        with pytest.raises(LoopLimit, match="stage_deadline"):
            budget.check()
        budget.begin_answer()
        with model_stage("answer"):
            budget.check()
            await asyncio.sleep(0.03)
            with pytest.raises(LoopLimit, match="stage_deadline"):
                budget.check()

    with asyncio.Runner(loop_factory=lambda: new_loop(loop_kind)) as runner:
        runner.run(scenario())


@pytest.mark.parametrize("loop_kind", LOOP_KINDS)
def test_tool_deadline_rejects_expired_and_cancels_running_call(loop_kind):
    async def scenario():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        contract = next(c for c in default_tool_contracts() if c.llm_name == "order_lookup")
        catalog = ToolCatalog([BoundTool(contract, handler)])
        loop = asyncio.get_running_loop()
        query = adapter(catalog, before_attempt=lambda op: loop.time() - 1)
        op = query.prepare("g1", "order_lookup", {"order_id": 8201})
        with pytest.raises(AdmissionError, match="deadline_exhausted"):
            await query.execute(op)
        assert not entered.is_set() and query.attempts(op) == ()
        query = adapter(catalog, before_attempt=lambda op: loop.time() + 0.02)
        op = query.prepare("g1", "order_lookup", {"order_id": 8201})
        record = await asyncio.wait_for(query.execute(op), 1)
        assert entered.is_set() and cleaned.is_set()
        assert not record.result.ok
        assert record.result.error.code == "deadline_exhausted"
        assert len(query.attempts(op)) == 1

    with asyncio.Runner(loop_factory=lambda: new_loop(loop_kind)) as runner:
        runner.run(scenario())


@pytest.mark.parametrize("loop_kind", LOOP_KINDS)
@pytest.mark.parametrize("mode", ["failure", "cancel"])
def test_failed_turn_cleanup_under_server_clock(monkeypatch, loop_kind, mode):
    async def scenario():
        message = "请介绍可以提供哪些服务"
        runtime, provider, repo = runtime_fixture(monkeypatch, [
            register(goal(message, disposition="direct_response")),
            finish(),
            final_answer(),
        ])
        entered = asyncio.Event()

        async def fail_complete(*args):
            entered.set()
            await asyncio.sleep(0.001)
            if mode == "failure":
                raise RuntimeError("synthetic save failure")
            await asyncio.Future()

        monkeypatch.setattr(runtime.context, "complete_turn", fail_complete)
        task = asyncio.create_task(runtime.run(ChatRequest(message=message), 41))
        await asyncio.wait_for(entered.wait(), 1)
        if mode == "cancel":
            task.cancel()
        with pytest.raises(RuntimeError if mode == "failure" else asyncio.CancelledError):
            await task
        assert len(provider.requests) == 3
        assert [m.role for m in repo.data["messages"]] == ["user"]
        assert repo.data["audit"] is None
        failure, = repo.data["failures"]
        assert failure["error"]["type"] == ("RuntimeError" if mode == "failure" else "cancelled")
        assert repo.session.rollbacks == 1

    with asyncio.Runner(loop_factory=lambda: new_loop(loop_kind)) as runner:
        runner.run(scenario())
