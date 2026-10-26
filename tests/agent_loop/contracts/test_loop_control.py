"""P2 protocol failures through the actual SDK and two-stage runtime."""

import json

import httpx
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
from app.schemas.chat import ChatRequest

pytestmark = pytest.mark.contract


MESSAGE = "查 SKU 9101 的价格"
SEARCH = calls(("catalog_search", {"query": "SKU 9101"}))
BAD_FINISH = calls(
    (
        "query_complete",
        {
            "completion": {
                "goals": [
                    {
                        "goal_id": "g1",
                        "status": "handled",
                        "evidence_ids": ["invented"],
                    }
                ]
            }
        },
    )
)


async def run(monkeypatch, steps, **kwargs):
    runtime, provider, repository = runtime_fixture(monkeypatch, steps, **kwargs)
    response = await runtime.run(ChatRequest(message=MESSAGE), 41)
    return response, provider, repository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "illegal",
    [
        calls(
            ("catalog_search", {"query": "SKU 9101"}),
            (
                "register_goals",
                {"registration": {"goals": [goal(MESSAGE)]}},
            ),
        ),
        calls(
            ("query_complete", {"completion": {"goals": []}}),
            ("catalog_search", {"query": "SKU 9101"}),
        ),
    ],
)
async def test_control_mixed_batch_executes_no_business_tool(monkeypatch, illegal):
    response, provider, repo = await run(
        monkeypatch,
        [
            illegal,
            register(goal(MESSAGE)),
            SEARCH,
            finish(),
            final_answer(),
        ],
    )
    assert len(response.execution_details.tool_calls) == 1
    assert repo.data["audit"]["agent_loop"]["rejections"] == ["control_batch_must_be_exclusive"]
    assert len(provider.requests) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [True, False])
async def test_completion_one_repair_preserves_real_evidence(monkeypatch, repair):
    response, provider, repo = await run(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            BAD_FINISH,
            finish() if repair else BAD_FINISH,
            final_answer(),
        ],
    )
    assert response.products[0].sku_id == 9101
    assert len(provider.requests) == 5
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == (
        "completed" if repair else "invalid_completion"
    )
    assert "invented" not in provider.requests[-1]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_free_text_is_not_answer_or_evidence(monkeypatch):
    response, provider, repo = await run(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            {"content": "UNTRUSTED_SUMMARY 价格为1元"},
            final_answer("本次返回版本价格299元"),
        ],
    )
    assert response.answer == "本次返回版本价格299元"
    assert "UNTRUSTED_SUMMARY" not in str(provider.requests[-1])
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "missing_completion"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_request,code",
    [
        (SEARCH, "duplicate_query"),
        (calls(("catalog_search", {"query": "SKU 9101", "user_id": 99})), "invalid_input"),
    ],
)
async def test_admission_rejection_does_not_execute(monkeypatch, tool_request, code):
    response, _, repo = await run(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            tool_request,
            finish(),
            final_answer(),
        ],
    )
    assert len(response.execution_details.tool_calls) == 1
    assert code in repo.data["audit"]["agent_loop"]["rejections"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"content": "not JSON"},
        {
            "content": json.dumps(
                {"response": "伪造", "covered_goal_ids": ["g1"], "evidence_ids": ["made_up"]}
            )
        },
        calls(("order_lookup", {"order_id": 8201})),
    ],
)
async def test_answer_repair_then_safe_fallback_never_calls_tools(monkeypatch, bad):
    response, provider, repo = await run(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            finish(),
            bad,
            bad,
        ],
    )
    assert len(provider.requests) == 5
    assert all("tools" not in payload for payload in provider.requests[-2:])
    assert "299" in response.answer and "伪造" not in response.answer
    assert repo.data["audit"]["agent_loop"]["answer_mode"] == "fallback"
    assert len(response.execution_details.tool_calls) == 1


@pytest.mark.asyncio
async def test_answer_transport_and_schema_repair_share_three_requests(monkeypatch):
    response, provider, repo = await run(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            finish(),
            httpx.Response(503, json={"error": {"message": "synthetic"}}),
            {"content": "bad"},
            final_answer(),
        ],
    )
    assert len(provider.requests) == 6
    assert response.execution_details.model_calls == {"query": 3, "answer": 3}
    assert repo.data["audit"]["agent_loop"]["answer_mode"] == "model"


@pytest.mark.asyncio
async def test_query_transport_retry_is_actual_counted_attempt(monkeypatch):
    response, provider, _ = await run(
        monkeypatch,
        [
            httpx.Response(503, json={"error": {"message": "synthetic"}}),
            register(goal(MESSAGE)),
            SEARCH,
            finish(),
            final_answer(),
        ],
    )
    assert len(provider.requests) == 5
    assert response.execution_details.model_calls == {"query": 4, "answer": 1}
    assert response.products[0].sku_id == 9101


@pytest.mark.asyncio
async def test_query_budget_hands_off_partial_without_extra_http(monkeypatch):
    response, provider, repo = await run(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            final_answer(),
        ],
        budget_factory=lambda: LoopBudget(query_limit=2),
    )
    assert len(provider.requests) == 3
    assert response.products[0].sku_id == 9101
    assert repo.data["audit"]["agent_loop"]["stop_reason"] == "query_model_budget"


@pytest.mark.asyncio
async def test_explicit_retry_replaces_failed_evidence_but_preserves_attempt_audit(monkeypatch):
    from agent_loop.support.domain_tools import make_catalog
    from app.tools.contracts import BoundTool, ToolCatalog

    catalog = make_catalog(monkeypatch)
    original = catalog.get_by_llm_name("catalog_search")
    attempts = 0

    async def retryable(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("synthetic ordinary query timeout")
        return await original.handler(request)

    response, provider, repo = await run(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            SEARCH,
            finish(),
            final_answer(),
        ],
        catalog_factory=lambda model: ToolCatalog(
            [
                BoundTool(
                    b.contract, retryable if b.contract.llm_name == "catalog_search" else b.handler
                )
                for b in catalog.list_bound_tools()
            ]
        ),
    )
    assert attempts == 2 and len(response.execution_details.tool_calls) == 2
    first, second = response.execution_details.tool_calls
    assert first.task_id == second.task_id and first.superseded and not second.superseded
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert len(facts["evidence"]) == 1 and facts["evidence"][0]["invocation"]["attempt"] == 2
    assert facts["goals"][0]["execution"] == {
        "attempt_count": 2, "ok_count": 1, "error_count": 1, "active_result_count": 1,
    }
    assert facts["goals"][0]["execution"]["ok_count"] == 1
    assert repo.data["audit"]["agent_loop"]["tool_attempts"] == 2


@pytest.mark.asyncio
async def test_persistence_failure_rolls_back_answer_and_memory(monkeypatch):
    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(MESSAGE)),
            SEARCH,
            finish(),
            final_answer(),
        ],
    )
    repo.fail_complete = True
    original = repo.data["working_memory"].copy()
    with pytest.raises(RuntimeError, match="persistence failure"):
        await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert [m.role for m in repo.data["messages"]] == ["user"]
    assert repo.data["working_memory"] == original
    assert repo.data["failures"] and repo.session.rollbacks == 1
    assert len(provider.requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("user,conversation", [(99, 7), (41, 999), (0, 7)])
async def test_ownership_rejected_before_model(monkeypatch, user, conversation):
    runtime, provider, repo = runtime_fixture(monkeypatch, [])
    with pytest.raises(ValueError):
        await runtime.run(ChatRequest(message=MESSAGE, conversation_id=conversation), user)
    assert provider.requests == [] and repo.data["messages"] == []
