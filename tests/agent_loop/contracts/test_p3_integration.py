"""P3 response, audit, historical metadata and explicit integration safeguards."""

import json
import subprocess
from pathlib import Path

import pytest
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from app.core.loop_budget import LoopBudget
from app.core.loop_models import LoopUsage
from app.schemas.chat import ChatRequest
from app.schemas.context import WorkingMemoryV2

from tests.integration.postgres_app import isolated_url

pytestmark = pytest.mark.contract


@pytest.mark.asyncio
async def test_partial_usage_stays_unknown_in_attempt_audit():
    usage = LoopUsage(LoopBudget())
    usage.attempts = 1
    usage.usage = [{"prompt_tokens": 10, "completion_tokens": None}]
    usage.stages = ["query"]
    usage.attempt_details = [{"stage": "query", "status_code": 200, "elapsed_seconds": 1}]
    result = usage.summary()
    assert result["usage_missing_attempts"] == 1
    assert result["attempts"][0]["usage_status"] == "unknown"


@pytest.mark.asyncio
async def test_live_comparison_retains_usage_when_response_projection_fails(monkeypatch):
    from agent_loop.support.strands_provider import SETTINGS

    from evals.agent import compare_runtimes

    usage = {
        "http_attempts": 4,
        "known_input_tokens": 100,
        "known_output_tokens": 20,
        "usage_missing_attempts": 0,
    }

    class FailedRuntime:
        def __init__(self, *args, context_service, **kwargs):
            self.context = context_service

        async def run(self, *args):
            await self.context.fail_turn(None, "ValueError", {"usage": usage})
            raise ValueError("synthetic response projection failure")

    monkeypatch.setattr(compare_runtimes, "AgentLoopRuntime", FailedRuntime)
    result = await compare_runtimes.evaluate_new({"id": "failure", "message": "查商品"}, SETTINGS)
    assert result["error_type"] == "ValueError"
    assert result["state"]["agent_loop"]["usage"] == usage
    rows = [
        {"id": "failure", "repeat": 0, "runtime": name, "usage": usage, "seconds": 1,
         **({"error_type": "ValueError"} if name == "strands" else {})}
        for name in ("legacy", "strands")
    ]
    assert compare_runtimes.comparison(rows)["cost_gates"]["no_exceptions"] is False


@pytest.mark.parametrize(
    "url",
    [
        "",
        "postgresql://localhost/pc_agent",
        "postgresql://localhost:5432/agent_loop_p3",
        "postgresql://example.com:15439/agent_loop_p3",
    ],
)
def test_live_database_entry_rejects_shared_or_unspecified_target(monkeypatch, url):
    monkeypatch.setenv("AGENT_LOOP_TEST_DATABASE_URL", url)
    with pytest.raises(ValueError):
        isolated_url()


def test_real_frontend_history_and_execution_render_contract():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        ["node", "tests/agent_loop/contracts/frontend_execution.mjs"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "contracts passed" in result.stdout


@pytest.mark.asyncio
async def test_legacy_candidate_ids_are_requeried_and_audit_has_safe_attempts(monkeypatch):
    message = "第二款的价格"
    memory = WorkingMemoryV2.model_validate({"catalog": {"candidate_sku_ids": [9101, 9102]}})
    runtime, _, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message, references=[{"kind": "catalog_candidate", "position": 2}])),
            calls(("catalog_search", {"query": "SKU 9102"})),
            finish(),
            final_answer(),
        ],
        working_memory=memory.model_dump(mode="json"),
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.products[0].sku_id == 9102
    call = response.execution_details.tool_calls[0]
    assert call.outcome == "usable" and call.result_summary["attempt"] == 1
    audit = repo.data["audit"]["agent_loop"]
    assert set(audit["timings"]) == {"prepare_seconds", "query_seconds", "answer_seconds"}
    attempts = audit["usage"]["attempts"]
    assert len(attempts) == 4 and attempts[-1]["stage"] == "answer"
    assert all(a["elapsed_seconds"] >= 0 and a["usage_status"] == "known" for a in attempts)
    assert "fixture-key" not in json.dumps(attempts)
    assert all(not ({"headers", "messages", "url", "body"} & set(a)) for a in attempts)


@pytest.mark.asyncio
async def test_not_found_is_displayed_as_business_outcome_not_execution_success(monkeypatch):
    message = "查 SKU 9999"
    runtime, _, _ = runtime_fixture(
        monkeypatch,
        [
            register(goal(message)),
            calls(("catalog_search", {"query": message})),
            finish(),
            final_answer(),
        ],
    )
    result = await runtime.run(ChatRequest(message=message), 41)
    assert result.products == []
    assert result.execution_details.tool_calls[0].outcome == "not_found"


@pytest.mark.asyncio
async def test_external_search_rejects_private_order_identifier(monkeypatch):
    message = "搜索订单 8201 的接口资料"
    runtime, _, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message)),
            calls(("knowledge_search", {"query": message})),
            finish(),
            final_answer(),
        ],
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.execution_details.tool_calls == []
    assert repo.data["audit"]["agent_loop"]["rejections"] == [
        "private_order_data_disallowed_in_web_query",
    ]
