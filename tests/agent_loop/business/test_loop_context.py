"""Multi-turn identities and current requirements over formal context/domain services."""

import json

import pytest
from agent_loop.support.domain_tools import make_catalog
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from app.schemas.catalog_search import SearchCondition, SearchPlan
from app.schemas.chat import ChatRequest
from app.schemas.context import WorkingMemoryV2
from app.tools.catalog.planner import CatalogSearchPlanner
from app.tools.contracts import BoundTool, ToolCatalog

pytestmark = [pytest.mark.business, pytest.mark.asyncio]


def previous_catalog():
    return WorkingMemoryV2.model_validate(
        {
            "catalog": {
                "query_plan": {"category": "键盘", "max_price": 500, "filters": {"wireless": True}},
                "candidate_sku_ids": [9101, 9102],
                "candidate_spu_ids": [91],
                "candidate_display": [
                    {"sku_id": 9101, "spu_id": 91, "title": "旧第一款"},
                    {"sku_id": 9102, "spu_id": 91, "title": "旧第二款"},
                ],
            }
        }
    ).model_dump(mode="json")


@pytest.mark.parametrize("message", ["第二款现在多少钱", "刚才列表中排第二的那个，重新确认价格"])
async def test_second_candidate_resolved_and_price_requeried(monkeypatch, message):
    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message, references=[{"kind": "catalog_candidate", "position": 2}])),
            calls(("catalog_search", {"query": "SKU 9102"})),
            finish(),
            final_answer(),
        ],
        working_memory=previous_catalog(),
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.products[0].sku_id == 9102 and str(response.products[0].price) == "199"
    assert len(response.execution_details.tool_calls) == 1
    assert repo.data["working_memory"]["catalog"]["referenced_sku_id"] == 9102
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert "旧第二款" not in str(facts["evidence"])


@pytest.mark.parametrize(
    "message,query,plan,expected",
    [
        ("换成鼠标看看", "查鼠标", {"category": "鼠标", "category_evidence": "鼠标"}, []),
        (
            "这次不要无线，给我有线键盘",
            "查有线键盘，连接USB",
            {
                "category": "键盘",
                "category_evidence": "键盘",
                "conditions": [
                    {"key": "connection_type", "values": ["USB"], "evidence": "USB"},
                ],
            },
            [9102],
        ),
    ],
)
async def test_current_requirements_replace_old_filters_with_formal_search(
    monkeypatch,
    message,
    query,
    plan,
    expected,
):
    captured = []

    async def fixed_plan(self, request):
        captured.append(request)
        return SearchPlan.model_validate(plan)

    monkeypatch.setattr(CatalogSearchPlanner, "plan", fixed_plan)
    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message)),
            calls(("catalog_search", {"query": query})),
            finish(),
            final_answer(),
        ],
        working_memory=previous_catalog(),
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert [p.sku_id for p in response.products] == expected
    assert captured[0].query == query
    assert all("wireless" not in c.key for c in SearchPlan.model_validate(plan).conditions)
    assert repo.data["memory"] == []  # One-turn criteria do not become permanent preferences.
    assert repo.data["working_memory"]["catalog"]["query_plan"]["category"] == plan["category"]
    assert len(provider.requests) == 4


async def test_cheaper_variant_refreshes_reference_price_before_filtering(monkeypatch):
    message = "第一款同系列有没有更便宜的版本"
    original = CatalogSearchPlanner.plan

    async def plan(self, request):
        if request.query == "SKU 9101":
            return await original(self, request)
        return SearchPlan(
            category="键盘",
            category_evidence="键盘",
            conditions=[
                SearchCondition(key="price", operator="lt", values=["299"], evidence="低于299元"),
            ],
        )

    monkeypatch.setattr(CatalogSearchPlanner, "plan", plan)

    def cheaper(payload):
        from agent_loop.support.loop_runtime import observations

        # The rewritten request derives its price from this turn's actual tool result.
        current = observations(payload)[0]["result"]["output"]["products"][0]
        assert current["price"] == "299"
        return calls(
            (
                "catalog_search",
                {
                    "query": "SPU 91 中价格低于299元的键盘版本",
                    "scope": {"spu_ids": [91]},
                },
            )
        )

    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message, references=[{"kind": "catalog_candidate", "position": 1}])),
            calls(("catalog_search", {"query": "SKU 9101"})),
            cheaper,
            finish(),
            final_answer(),
        ],
        working_memory=previous_catalog(),
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    facts = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert facts["evidence"][1]["result"]["output"]["products"][0]["sku_id"] == 9102
    assert len(response.execution_details.tool_calls) == 2
    assert repo.data["memory"] == []


@pytest.mark.parametrize("failure", [False, True])
async def test_empty_clears_candidates_but_tool_failure_preserves_them(monkeypatch, failure):
    message = "查 SKU 9999"
    runtime, _, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message)),
            calls(("catalog_search", {"query": message})),
            finish(),
            final_answer(),
        ],
        working_memory=previous_catalog(),
    )
    if failure:
        catalog = make_catalog(monkeypatch)

        async def fail(request):
            raise RuntimeError("synthetic unavailable")

        runtime.catalog_factory = lambda model: ToolCatalog(
            [
                BoundTool(
                    b.contract, fail if b.contract.llm_name == "catalog_search" else b.handler
                )
                for b in catalog.list_bound_tools()
            ]
        )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.products == []
    assert repo.data["working_memory"]["catalog"]["candidate_sku_ids"] == (
        [9101, 9102] if failure else []
    )


async def test_old_memory_identity_upgraded_then_dynamic_order_requeried(monkeypatch):
    message = "上一单现在发货了吗"
    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message, references=[{"kind": "last_order"}])),
            calls(("order_lookup", {"order_id": 8201})),
            finish(),
            final_answer(),
        ],
        working_memory={"last_order_id": 8201, "last_intent": "order_status"},
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.order.id == 8201 and response.order.status_label == "已发货"
    assert repo.data["working_memory"]["schema_version"] == 2
    assert len(provider.requests) == 4


@pytest.mark.parametrize(
    "message,expected", [("请记住，以后我偏好无线设备", 1), ("本轮我想看无线设备", 0)]
)
async def test_only_explicit_stable_preferences_written(monkeypatch, message, expected):
    runtime, _, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message, disposition="direct_response")),
            finish(),
            final_answer(),
        ],
    )
    response = await runtime.run(ChatRequest(message=message), 41)
    assert len(repo.data["memory"]) == expected
    assert len(response.memory_changes) == expected


async def test_forgotten_preference_is_not_carried_by_runtime_instance(monkeypatch):
    message = "说说当前选购方向"
    preference = {
        "id": 12,
        "key": "connection_preference",
        "value": "偏好无线设备",
        "value_json": {"preference": "wireless"},
        "confidence": 0.8,
        "scope": "shopping",
        "fact_type": "preference",
    }
    steps = [
        register(goal(message, disposition="direct_response")),
        finish(),
        final_answer(),
    ]
    runtime, provider, repo = runtime_fixture(monkeypatch, steps * 2, memory=[preference])
    await runtime.run(ChatRequest(message=message), 41)
    assert "偏好无线设备" in str(provider.requests[0])
    # Simulates the existing memory API deletion, not a new chat write operation.
    repo.data["memory"] = []
    await repo.session.commit()
    await runtime.run(ChatRequest(message=message), 41)
    assert "偏好无线设备" not in str(provider.requests[3])
