import json
import re
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import AgentRuntime
from app.core.config import Settings
from app.models import Category, Sku, Spu
from app.schemas.chat import ChatRequest
from app.schemas.context import MemoryChanges, PreparedTurn, WorkingMemoryV2
from app.tools.contracts import RegistryToolExecutor, ToolContract
from app.tools.schemas import ToolExecutionResult


def _runtime_catalog_sku_ids(runtime_context: dict[str, Any]) -> list[int]:
    catalog_context = runtime_context["catalog_context"]
    memory = catalog_context["memory"]
    values: list[int] = []
    if memory.get("referenced_sku_id") is not None:
        values.append(memory["referenced_sku_id"])
    for artifact in catalog_context["dependency_artifacts"]:
        values.extend(artifact["selected_sku_ids"])
    return list(dict.fromkeys(values))


def _route_message(rewritten_query: str, goals: list[dict[str, Any]]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "route-e2e",
                "name": "route_request",
                "args": {
                    "rewritten_query": rewritten_query,
                    "subqueries": goals,
                },
                "type": "tool_call",
            }
        ],
    )


class _RouterModel:
    def __init__(self, response: AIMessage):
        self.response = response
        self.call_count = 0

    def bind_tools(self, _: list[dict[str, Any]], **__: Any) -> "_RouterModel":
        return self

    async def ainvoke(self, _: list[Any]) -> AIMessage:
        self.call_count += 1
        return self.response


def _tagged_json(content: str, tag: str) -> Any:
    match = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", content, re.DOTALL)
    if match is None:
        raise AssertionError(f"answer prompt is missing <{tag}>")
    return json.loads(match.group(1))


class _ArtifactAnswerModel:
    """A schema-constrained answer double that can only read the Artifact prompt."""

    def __init__(self):
        self.answer_call_count = 0
        self.planner_call_count = 0
        self.last_used_tool_call_ids: list[str] = []

    def bind_tools(self, tools: list[dict[str, Any]], **_: Any) -> "_BoundArtifactAnswerModel":
        names = {str(item["function"]["name"]) for item in tools}
        role = (
            "answer"
            if names
            & {
                "finish_answer",
                "finish_partial",
                "finish_unavailable",
                "ask_clarification",
            }
            else "planner"
        )
        return _BoundArtifactAnswerModel(self, role)

    def answer(self, messages: list[Any]) -> AIMessage:
        self.answer_call_count += 1
        content = str(messages[-1].content)
        answer_context = _tagged_json(content, "answer_context")
        parts: list[str] = []
        used_ids: list[str] = []

        for task in answer_context["tasks"]:
            artifact = task["artifact"]
            assert task["semantic_outcome"] == "answered_with_facts"
            used_ids.append(artifact["source_tool_call_id"])
            value = artifact["facts"]
            tool_name = artifact["source_tool_name"]
            if tool_name == "catalog_compare":
                if value.get("comparison_level") == "spu":
                    rendered = "；".join(
                        f"{item['title']}（{item['sku_count']} 个版本，"
                        f"¥{item['min_price']}–¥{item['max_price']}）"
                        for item in value["series"]
                    )
                else:
                    rendered = "；".join(
                        f"{item['title']}（SKU {item['sku_id']}，¥{item['price']}）"
                        for item in value["products"]
                    )
                parts.append(f"对比结果：{rendered}")
            elif tool_name == "catalog_search":
                product = value["products"][0]
                window = task["response_contract"].get("result_window", {})
                if (
                    window.get("result_purpose") == "recommendation"
                    and not window.get("is_exhaustive", True)
                ):
                    parts.append(
                        f"共匹配 {window['total_match_count']} 个版本，本次返回 "
                        f"{window['returned_count']} 个候选；首推：{product['title']}"
                        f"（SKU {product['sku_id']}，¥{product['price']}）"
                    )
                else:
                    parts.append(
                        f"推荐：{product['title']}（SKU {product['sku_id']}，"
                        f"¥{product['price']}）"
                    )
            elif tool_name in {"policy_search", "knowledge_search"}:
                snippets = "；".join(item["snippet"] for item in value["documents"])
                parts.append(f"政策：{snippets}")
            elif tool_name == "order_lookup":
                order = value["order"]
                parts.append(
                    f"订单 {order['id']} 当前状态为{order['status_label']}，"
                    f"实付 ¥{order['pay_amount']}。"
                )
            elif tool_name == "catalog_facets":
                values = "、".join(item["value"] for item in value["items"])
                parts.append(f"可选{value['facet']}：{values}")
            else:  # pragma: no cover - protects the test double's closed schema
                raise AssertionError(f"unsupported answer artifact: {tool_name}")

        self.last_used_tool_call_ids = used_ids
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "finish-e2e",
                    "name": "finish_answer",
                    "args": {
                        "response": "\n".join(parts),
                        "used_tool_call_ids": used_ids,
                    },
                    "type": "tool_call",
                }
            ],
        )

    def plan(self, messages: list[Any]) -> AIMessage:
        self.planner_call_count += 1
        content = str(messages[-1].content)
        tasks = _tagged_json(content, "routed_subqueries")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"planner_{task['id']}_{task['capability']}",
                    "name": task["capability"],
                    "args": {"subquery": task["id"]},
                    "type": "tool_call",
                }
                for task in tasks
            ],
        )


class _AutonomousArtifactModel(_ArtifactAnswerModel):
    """Choose a Catalog tool from a planner_required Task's frozen query."""

    def plan(self, messages: list[Any]) -> AIMessage:
        self.planner_call_count += 1
        content = str(messages[-1].content)
        tasks = _tagged_json(content, "routed_subqueries")
        calls: list[dict[str, Any]] = []
        for task in tasks:
            query = str(task["canonical_query"])
            if "订单" in query:
                tool_name = "order_lookup"
            elif "对比" in query or "比较" in query:
                tool_name = "catalog_compare"
            else:
                tool_name = "catalog_search"
            calls.append(
                {
                    "id": f"planner_{task['id']}_{tool_name}",
                    "name": tool_name,
                    "args": {"subquery": task["id"]},
                    "type": "tool_call",
                }
            )
        return AIMessage(content="", tool_calls=calls)


class _BoundArtifactAnswerModel:
    def __init__(self, owner: _ArtifactAnswerModel, role: str):
        self.owner = owner
        self.role = role

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        if self.role == "planner":
            return self.owner.plan(messages)
        return self.owner.answer(messages)


class _ContextService:
    def __init__(
        self,
        message: str,
        referenced_sku_id: int | None = None,
        *,
        referenced_spu_id: int | None = None,
        working_memory: WorkingMemoryV2 | None = None,
    ):
        if working_memory is None:
            working_memory = WorkingMemoryV2.model_validate(
                {
                    "catalog": {
                        "referenced_spu_id": referenced_spu_id,
                        "referenced_sku_id": referenced_sku_id,
                        "candidate_spu_ids": (
                            [referenced_spu_id] if referenced_spu_id is not None else []
                        ),
                        "candidate_sku_ids": (
                            [referenced_sku_id] if referenced_sku_id is not None else []
                        ),
                    }
                }
            )
        self.prepared = PreparedTurn(
            user_id=7,
            conversation_id=41,
            user_message_id=51,
            run_id=61,
            message=message,
            working_memory=working_memory,
        )
        self.outcomes: list[dict[str, Any]] = []

    async def prepare_turn(self, _: int, __: int | None, ___: str) -> PreparedTurn:
        return self.prepared

    async def complete_turn(self, _: PreparedTurn, outcome: dict[str, Any]) -> MemoryChanges:
        self.outcomes.append(outcome)
        return MemoryChanges(working_memory=self.prepared.working_memory)


def _product(
    sku_id: int,
    spu_id: int,
    title: str,
    price: str,
    *,
    category: str,
    sales_count: int,
    sku_sales_count: int,
) -> dict[str, Any]:
    return {
        "sku_id": sku_id,
        "spu_id": spu_id,
        "title": title,
        "brand": "E2E",
        "category": category,
        "price": price,
        "stock": 10,
        "sales_count": sales_count,
        "sku_sales_count": sku_sales_count,
        "specs": {},
        "image_url": None,
    }


_PRODUCTS = {
    101: _product(
        101,
        10,
        "Rank-1 Monitor",
        "2199.00",
        category="monitor",
        sales_count=9000,
        sku_sales_count=900,
    ),
    201: _product(
        201,
        20,
        "Rank-2 Monitor",
        "1899.00",
        category="monitor",
        sales_count=8000,
        sku_sales_count=800,
    ),
    301: _product(
        301,
        30,
        "Rank-3 Monitor",
        "1599.00",
        category="monitor",
        sales_count=7000,
        sku_sales_count=700,
    ),
    401: _product(
        401,
        40,
        "Keyboard Choice",
        "499.00",
        category="keyboard",
        sales_count=6000,
        sku_sales_count=600,
    ),
    501: _product(
        501,
        50,
        "Rank-1 Keyboard SKU",
        "799.00",
        category="keyboard",
        sales_count=9000,
        sku_sales_count=900,
    ),
    502: _product(
        502,
        50,
        "Rank-2 Keyboard SKU",
        "699.00",
        category="keyboard",
        sales_count=9000,
        sku_sales_count=800,
    ),
    503: _product(
        503,
        50,
        "Rank-3 Keyboard SKU",
        "599.00",
        category="keyboard",
        sales_count=9000,
        sku_sales_count=700,
    ),
    600: _product(
        600,
        60,
        "Current Keyboard",
        "899.00",
        category="keyboard",
        sales_count=5000,
        sku_sales_count=500,
    ),
    700: _product(
        700,
        70,
        "Current Mouse",
        "399.00",
        category="mouse",
        sales_count=4000,
        sku_sales_count=400,
    ),
    710: _product(
        710,
        71,
        "Rank-1 Mouse",
        "329.00",
        category="mouse",
        sales_count=8500,
        sku_sales_count=850,
    ),
    720: _product(
        720,
        72,
        "Rank-2 Mouse",
        "279.00",
        category="mouse",
        sales_count=7500,
        sku_sales_count=750,
    ),
    800: _product(
        800,
        80,
        "Current Headset",
        "699.00",
        category="headset",
        sales_count=4000,
        sku_sales_count=400,
    ),
    801: _product(
        801,
        81,
        "Rank-1 Headset",
        "799.00",
        category="headset",
        sales_count=8800,
        sku_sales_count=880,
    ),
    802: _product(
        802,
        82,
        "Rank-2 Headset",
        "599.00",
        category="headset",
        sales_count=7800,
        sku_sales_count=780,
    ),
    901: _product(
        901, 90, "Mouse Choice", "299.00", category="mouse", sales_count=5000, sku_sales_count=500
    ),
    990: _product(
        990,
        99,
        "Current Monitor",
        "1999.00",
        category="monitor",
        sales_count=4000,
        sku_sales_count=400,
    ),
}


class _ScenarioToolExecutor:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.runtime_contexts: list[dict[str, Any]] = []

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult:
        name = contract.name
        self.calls.append((name, dict(arguments)))
        self.runtime_contexts.append(dict(runtime_context))
        query = str(arguments.get("query") or "")

        if name == "catalog_search":
            if "显示器 SPU 销量排行第三" in query:
                products = [_PRODUCTS[301]]
            elif "推荐一个键盘" in query:
                products = [_PRODUCTS[401]]
            elif "推荐当前键盘最合适的版本" in query:
                products = [_PRODUCTS[501], _PRODUCTS[502], _PRODUCTS[503]]
            elif "键盘 SKU 销量排行第二" in query:
                products = [_PRODUCTS[502]]
            elif "键盘 SPU 销量排行第一" in query:
                products = [_PRODUCTS[501]]
            elif "鼠标 SPU 销量排行第一" in query:
                products = [_PRODUCTS[710]]
            elif "鼠标 SPU 销量排行第二" in query:
                products = [_PRODUCTS[720]]
            elif "耳机 SPU 销量排行第二" in query:
                products = [_PRODUCTS[802]]
            elif "推荐一个无线鼠标" in query:
                products = [_PRODUCTS[901]]
            elif "推荐一个显示器" in query:
                products = [_PRODUCTS[301]]
            elif "推荐一个鼠标" in query:
                products = [_PRODUCTS[901]]
            else:  # pragma: no cover - catches accidental query rewriting
                raise AssertionError(f"unexpected frozen catalog query: {query}")
            if "当前" in query and "返回两项商品结果" in query:
                current_ids = _runtime_catalog_sku_ids(runtime_context)
                current_products = [
                    _PRODUCTS[sku_id]
                    for sku_id in current_ids
                    if sku_id in _PRODUCTS
                ]
                products = list(
                    {
                        product["sku_id"]: product
                        for product in [*current_products, *products]
                    }.values()
                )
            output = {
                "result_type": "products",
                "products": products,
                "ranking_strategy": "fixture",
                "query_plan": {
                    "query": query,
                    **(
                        {
                            "ranking": {
                                "scope": "spu",
                                "metric": "sales",
                                "direction": "desc",
                                "rank": 2,
                                "count": 1,
                            }
                        }
                        if "耳机 SPU 销量排行第二" in query
                        else {}
                    ),
                },
                "diagnostics": [],
            }
            if "推荐当前键盘最合适的版本" in query:
                output.update(
                    {
                        "result_purpose": "recommendation",
                        "selection_scope": "sku",
                        "total_match_count": 12,
                        "returned_count": 3,
                        "is_exhaustive": False,
                    }
                )
        elif name == "catalog_compare":
            sku_ids = _runtime_catalog_sku_ids(runtime_context)
            output = {
                "result_type": "comparison",
                "products": [_PRODUCTS[sku_id] for sku_id in sku_ids],
                "comparison_fields": ["price", "sales_count"],
                "missing_fields": {},
                "query_plan": {"query": query},
                "diagnostics": [],
            }
        elif name == "policy_search":
            output = {
                "result_type": "documents",
                "documents": [
                    {
                        "source_type": "knowledge_document",
                        "source_id": 88,
                        "title": "退货政策",
                        "document_type": "policy",
                        "snippet": "符合条件的商品可在七天内申请退货。",
                        "score": 0.95,
                        "metadata": {},
                    }
                ],
                "search_strategy": "hybrid",
            }
        elif name == "knowledge_search":
            output = {
                "result_type": "documents",
                "documents": [
                    {
                        "source_type": "knowledge_document",
                        "source_id": 89,
                        "title": "外设选购知识",
                        "document_type": "knowledge",
                        "snippet": "选购时应结合连接方式、尺寸和使用场景。",
                        "score": 0.91,
                        "metadata": {},
                    }
                ],
                "search_strategy": "hybrid",
            }
        elif name == "catalog_facets":
            output = {
                "result_type": "facets",
                "facet": "brand",
                "items": [
                    {"value": "E2E Display", "count": 3},
                    {"value": "Fixture Vision", "count": 2},
                ],
                "query_plan": {"query": query},
                "diagnostics": [],
            }
        elif name == "order_lookup" and arguments.get("order_id") is None:
            candidates = [
                {
                    "id": 4321,
                    "status": 2,
                    "status_label": "已发货",
                    "pay_amount": "399.00",
                    "created_at": "2026-07-20T12:00:00",
                    "item_count": 1,
                    "first_item_name": "鼠标",
                    "logistic_no": "SF123",
                }
            ]
            if "两笔" in query:
                candidates.append(
                    {
                        "id": 4322,
                        "status": 3,
                        "status_label": "已完成",
                        "pay_amount": "699.00",
                        "created_at": "2026-07-19T12:00:00",
                        "item_count": 1,
                        "first_item_name": "键盘",
                        "logistic_no": "YT456",
                    }
                )
            output = {
                "result_type": "order_candidates",
                "order": None,
                "candidates": candidates,
                "focus_order_ids": [item["id"] for item in candidates],
            }
        elif name == "order_lookup" and arguments.get("order_id") in {4321, 4322}:
            order_id = int(arguments["order_id"])
            second = order_id == 4322
            output = {
                "result_type": "single_order",
                "order": {
                    "id": order_id,
                    "status": 3 if second else 2,
                    "status_label": "已完成" if second else "已发货",
                    "pay_amount": "699.00" if second else "399.00",
                    "created_at": (
                        "2026-07-19T12:00:00" if second else "2026-07-20T12:00:00"
                    ),
                    "items": [
                        {
                            "id": 1,
                            "sku_id": 401 if second else 901,
                            "sku_name": "Keyboard Choice" if second else "Mouse Choice",
                            "sku_specs": {},
                            "price": "699.00" if second else "399.00",
                            "quantity": 1,
                        }
                    ],
                    "logistics": {
                        "express_company": "圆通" if second else "顺丰",
                        "logistic_no": "YT456" if second else "SF123",
                        "status": 3 if second else 2,
                        "trace": [],
                    },
                },
                "candidates": [],
            }
        else:  # pragma: no cover - closes the fake tool surface
            raise AssertionError(f"unexpected tool call: {name} {arguments}")

        return ToolExecutionResult(
            tool_name=contract.registry_name,
            ok=True,
            output=output,
        )


class _AuditRepository:
    def __init__(self, _: AsyncSession):
        pass

    async def add_tool_call(self, *_: Any) -> None:
        pass


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message: str,
    rewritten_query: str,
    goals: list[dict[str, Any]],
    referenced_sku_id: int | None = None,
    answer_model: _ArtifactAnswerModel | None = None,
) -> tuple[
    AgentRuntime,
    _RouterModel,
    _ArtifactAnswerModel,
    _ScenarioToolExecutor,
    _ContextService,
]:
    monkeypatch.setattr("app.agent.graph.ConversationRepository", _AuditRepository)
    router = _RouterModel(_route_message(rewritten_query, goals))
    answer = answer_model or _ArtifactAnswerModel()
    executor = _ScenarioToolExecutor()
    context = _ContextService(message, referenced_sku_id)
    runtime = AgentRuntime(
        cast(AsyncSession, None),
        Settings(llm_api_key=""),
        chat_model=answer,
        router_model=router,
        context_service=context,
        tool_executor=executor,
    )
    return runtime, router, answer, executor, context


@pytest.mark.asyncio
async def test_e2e_recommendation_window_reaches_answer_as_total_and_primary_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "这个键盘你最推荐哪个版本"
    goals = [
        {
            "id": "goal_99",
            "query": message,
            "disposition": "tool_planning",
            "reason_code": "recommend_variant",
            "tasks": [
                {
                    "id": "task_99",
                    "goal_id": "goal_99",
                    "canonical_query": "推荐当前键盘最合适的版本",
                    "depends_on": [],
                    "input_requirements": [
                        {"name": "current", "source": "context_product"}
                    ],
                    "produces": "products",
                    "answer_role": "user_facing",
                    "capability": "catalog_search",
                }
            ],
        }
    ]
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query=message,
        goals=goals,
        referenced_sku_id=600,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]
    artifact = state["task_artifacts"]["task_99"]["value"]

    assert artifact["result_purpose"] == "recommendation"
    assert artifact["selection_scope"] == "sku"
    assert artifact["total_match_count"] == 12
    assert artifact["returned_count"] == 3
    assert artifact["is_exhaustive"] is False
    assert state["parsed"]["product_search"]["total_match_count"] == 12
    assert state["parsed"]["product_search"]["result_purpose"] == "recommendation"
    assert "共匹配 12 个版本，本次返回 3 个候选" in response.answer
    assert "首推：Rank-1 Keyboard SKU" in response.answer
    assert "只有三个版本" not in response.answer
    assert _runtime_catalog_sku_ids(executor.runtime_contexts[0]) == [600]
    _assert_successful_e2e(
        state,
        router,
        answer,
        context,
        expected_tool_waves=1,
    )


@pytest.mark.asyncio
async def test_e2e_out_of_order_spu_rank_compare_and_independent_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "对比这个和销量第三的显示器，再推荐一个键盘"
    goals = [
        {
            "id": "goal_7",
            "query": "对比当前显示器和销量第三的显示器",
            "disposition": "tool_planning",
            "reason_code": "compare_third_monitor",
            "tasks": [
                    {
                        "id": "task_8",
                        "goal_id": "goal_7",
                        "canonical_query": "仅比较 task_7 返回的两个显示器",
                        "depends_on": ["task_7"],
                        "input_requirements": [],
                    "produces": "comparison",
                    "answer_role": "user_facing",
                    "capability": "catalog_compare",
                },
                    {
                        "id": "task_7",
                        "goal_id": "goal_7",
                        "canonical_query": (
                            "查询当前商品以及显示器 SPU 销量排行第三的商品，"
                            "返回两项商品结果"
                        ),
                        "depends_on": [],
                        "input_requirements": [],
                        "produces": "products",
                        "answer_role": "internal",
                        "capability": "catalog_search",
                },
            ],
        },
        {
            "id": "goal_8",
            "query": "推荐一个键盘",
            "disposition": "tool_planning",
            "reason_code": "recommend_keyboard",
            "tasks": [
                {
                    "id": "task_9",
                    "goal_id": "goal_8",
                    "canonical_query": "推荐一个键盘",
                    "depends_on": [],
                    "input_requirements": [],
                    "produces": "products",
                    "answer_role": "user_facing",
                    "capability": "catalog_search",
                }
            ],
        },
    ]
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query="对比当前显示器和销量第三的显示器，再推荐一个键盘",
        goals=goals,
        referenced_sku_id=990,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert [[call["subquery"] for call in wave["calls"]] for wave in state["tool_waves"]] == [
        ["task_7", "task_9"],
        ["task_8"],
    ]
    assert executor.calls[-1][1] == {
        "query": "仅比较 task_7 返回的两个显示器",
        "limit": 5,
    }
    assert _runtime_catalog_sku_ids(executor.runtime_contexts[-1]) == [990, 301]
    assert "Rank-3 Monitor（SKU 301，¥1599.00）" in response.answer
    assert "Keyboard Choice（SKU 401，¥499.00）" in response.answer
    assert answer.last_used_tool_call_ids == [
        "router_task_8_catalog_compare",
        "router_task_9_catalog_search",
    ]
    _assert_successful_e2e(state, router, answer, context)


@pytest.mark.asyncio
async def test_e2e_sku_rank_compare_and_policy_share_first_wave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "对比这个和 SKU 销量第二的版本，并查询退货政策"
    goals = [
        {
            "id": "goal_3",
            "query": "对比当前键盘和 SKU 销量第二的版本",
            "disposition": "tool_planning",
            "reason_code": "compare_second_sku",
            "tasks": [
                    {
                        "id": "task_4",
                        "goal_id": "goal_3",
                        "canonical_query": (
                            "查询当前商品以及键盘 SKU 销量排行第二的版本，"
                            "返回两项商品结果"
                        ),
                    "depends_on": [],
                    "input_requirements": [],
                        "produces": "products",
                    "answer_role": "internal",
                    "capability": "catalog_search",
                    },
                    {
                        "id": "task_5",
                        "goal_id": "goal_3",
                        "canonical_query": "仅比较 task_4 返回的两个键盘版本",
                        "depends_on": ["task_4"],
                        "input_requirements": [],
                    "produces": "comparison",
                    "answer_role": "user_facing",
                    "capability": "catalog_compare",
                },
            ],
        },
        {
            "id": "goal_4",
            "query": "查询退货政策",
            "disposition": "tool_planning",
            "reason_code": "return_policy",
            "tasks": [
                {
                    "id": "task_6",
                    "goal_id": "goal_4",
                    "canonical_query": "查询退货政策",
                    "depends_on": [],
                    "input_requirements": [],
                    "produces": "documents",
                    "answer_role": "user_facing",
                    "capability": "policy_search",
                }
            ],
        },
    ]
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query="对比当前键盘和 SKU 销量第二的版本，并查询退货政策",
        goals=goals,
        referenced_sku_id=600,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert [[call["subquery"] for call in wave["calls"]] for wave in state["tool_waves"]] == [
        ["task_4", "task_6"],
        ["task_5"],
    ]
    assert executor.calls[-1][1] == {
        "query": "仅比较 task_4 返回的两个键盘版本",
        "limit": 5,
    }
    assert _runtime_catalog_sku_ids(executor.runtime_contexts[-1]) == [600, 502]
    assert "Rank-2 Keyboard SKU（SKU 502，¥699.00）" in response.answer
    assert "符合条件的商品可在七天内申请退货" in response.answer
    assert response.evidence[0].source_id == 88
    assert state["task_artifacts"]["task_6"]["source_tool_call_id"] in (
        answer.last_used_tool_call_ids
    )
    _assert_successful_e2e(state, router, answer, context)


@pytest.mark.asyncio
async def test_e2e_order_candidate_binds_detail_and_catalog_stays_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "查询最近订单详情，再推荐一个鼠标"
    goals = [
        {
            "id": "goal_10",
            "query": "查询最近订单详情",
            "disposition": "tool_planning",
            "reason_code": "latest_order_detail",
            "tasks": [
                {
                    "id": "task_10",
                    "goal_id": "goal_10",
                    "canonical_query": "查询最近订单候选",
                    "depends_on": [],
                    "input_requirements": [],
                    "produces": "order",
                    "answer_role": "internal",
                    "capability": "order_lookup",
                },
                {
                    "id": "task_11",
                    "goal_id": "goal_10",
                    "canonical_query": "查询选中订单的详情",
                    "depends_on": ["task_10"],
                    "input_requirements": [
                        {"name": "order_id", "source": "task_output", "task_id": "task_10"}
                    ],
                    "produces": "order",
                    "answer_role": "user_facing",
                    "capability": "order_lookup",
                },
            ],
        },
        {
            "id": "goal_11",
            "query": "推荐一个鼠标",
            "disposition": "tool_planning",
            "reason_code": "recommend_mouse",
            "tasks": [
                {
                    "id": "task_12",
                    "goal_id": "goal_11",
                    "canonical_query": "推荐一个鼠标",
                    "depends_on": [],
                    "input_requirements": [],
                    "produces": "products",
                    "answer_role": "user_facing",
                    "capability": "catalog_search",
                }
            ],
        },
    ]
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query=message,
        goals=goals,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert [[call["subquery"] for call in wave["calls"]] for wave in state["tool_waves"]] == [
        ["task_10", "task_12"],
        ["task_11"],
    ]
    assert executor.calls[-1][1]["order_id"] == 4321
    assert "订单 4321 当前状态为已发货" in response.answer
    assert "Mouse Choice（SKU 901，¥299.00）" in response.answer
    assert response.order is not None
    assert response.order.id == 4321
    assert "router_task_10_order_lookup" not in answer.last_used_tool_call_ids
    _assert_successful_e2e(state, router, answer, context)


@pytest.mark.asyncio
async def test_e2e_depends_on_only_passes_second_order_candidate_to_detail_wave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.agent.graph._deterministic_pre_route_plan", lambda _: None)
    message = "先列出最近两笔订单，再查询第二笔订单的物流详情"
    goals = [
        {
            "id": "goal_12",
            "query": message,
            "disposition": "tool_planning",
            "reason_code": "list_then_select_order",
            "tasks": [
                {
                    "id": "task_13",
                    "goal_id": "goal_12",
                    "canonical_query": "列出最近两笔订单",
                    "depends_on": [],
                    "input_requirements": [],
                    "produces": "result",
                    "answer_role": "internal",
                    "capability": "planner_required",
                },
                {
                    "id": "task_14",
                    "goal_id": "goal_12",
                    "canonical_query": "查询第二笔订单的物流详情",
                    "depends_on": ["task_13"],
                    "input_requirements": [],
                    "produces": "result",
                    "answer_role": "user_facing",
                    "capability": "planner_required",
                },
            ],
        }
    ]
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query=message,
        goals=goals,
        answer_model=_AutonomousArtifactModel(),
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert [
        [(call["subquery"], call["name"]) for call in wave["calls"]]
        for wave in state["tool_waves"]
    ] == [
        [("task_13", "order_lookup")],
        [("task_14", "order_lookup")],
    ]
    assert executor.calls[-1][1]["order_id"] == 4322
    assert state["route_plan"]["subqueries"][0]["tasks"][1]["input_requirements"] == []
    assert state["task_status"]["task_14"]["status"] == "succeeded"
    assert "订单 4322 当前状态为已完成" in response.answer
    _assert_successful_e2e(
        state,
        router,
        answer,
        context,
        expected_planner_calls=2,
    )


def _assert_successful_e2e(
    state: dict[str, Any],
    router: _RouterModel,
    answer: _ArtifactAnswerModel,
    context: _ContextService,
    *,
    expected_planner_calls: int = 0,
    expected_tool_waves: int = 2,
    expected_boundary: str = "in_scope_auto",
) -> None:
    assert router.call_count == 1
    assert answer.planner_call_count == expected_planner_calls
    assert answer.answer_call_count == 1
    assert len(context.outcomes) == 1
    assert state["tool_wave_count"] == expected_tool_waves
    assert state["terminal_guard_status"] == "accepted"
    assert all(item["status"] == "succeeded" for item in state["task_status"].values())
    assert state["boundary"]["classification"] == expected_boundary


def _human_handoff_goal(goal_id: str, query: str, reason_code: str) -> dict[str, Any]:
    return {
        "id": goal_id,
        "query": query,
        "disposition": "human_handoff",
        "reason_code": reason_code,
    }


def _spu_rank_compare_goal() -> dict[str, Any]:
    return {
        "id": "goal_20",
        "query": "对比当前显示器和销量第三的显示器",
        "disposition": "tool_planning",
        "reason_code": "compare_third_monitor",
        "tasks": [
            {
                "id": "task_20",
                "goal_id": "goal_20",
                "canonical_query": (
                    "查询当前商品以及显示器 SPU 销量排行第三的商品，"
                    "返回两项商品结果"
                ),
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "internal",
                "capability": "catalog_search",
            },
            {
                "id": "task_21",
                "goal_id": "goal_20",
                "canonical_query": "仅比较 task_20 返回的两个显示器",
                "depends_on": ["task_20"],
                "input_requirements": [],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }


def _sku_rank_compare_goal() -> dict[str, Any]:
    return {
        "id": "goal_22",
        "query": "对比当前键盘和 SKU 销量第二的版本",
        "disposition": "tool_planning",
        "reason_code": "compare_second_sku",
        "tasks": [
            {
                "id": "task_22",
                "goal_id": "goal_22",
                "canonical_query": (
                    "查询当前商品以及键盘 SKU 销量排行第二的版本，"
                    "返回两项商品结果"
                ),
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "internal",
                "capability": "catalog_search",
            },
            {
                "id": "task_23",
                "goal_id": "goal_22",
                "canonical_query": "仅比较 task_22 返回的两个键盘版本",
                "depends_on": ["task_22"],
                "input_requirements": [],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }


def _order_detail_goal() -> dict[str, Any]:
    return {
        "id": "goal_24",
        "query": "查询最近订单详情",
        "disposition": "tool_planning",
        "reason_code": "latest_order_detail",
        "tasks": [
            {
                "id": "task_24",
                "goal_id": "goal_24",
                "canonical_query": "查询最近订单候选",
                "depends_on": [],
                "input_requirements": [],
                "produces": "order",
                "answer_role": "internal",
                "capability": "order_lookup",
            },
            {
                "id": "task_25",
                "goal_id": "goal_24",
                "canonical_query": "查询选中订单的详情",
                "depends_on": ["task_24"],
                "input_requirements": [
                    {
                        "name": "order_id",
                        "source": "task_output",
                        "task_id": "task_24",
                    }
                ],
                "produces": "order",
                "answer_role": "user_facing",
                "capability": "order_lookup",
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "message",
        "admitted_goal",
        "capability_gap_goal",
        "referenced_sku_id",
        "expected_waves",
        "expected_answer",
        "expected_bound_argument",
        "internal_call_id",
    ),
    [
        pytest.param(
            "对比这个和销量第三的显示器，另外帮我取消最近的订单",
            _spu_rank_compare_goal(),
            _human_handoff_goal("goal_21", "帮我取消最近的订单", "cancel_order"),
            990,
            [["task_20"], ["task_21"]],
            "Rank-3 Monitor（SKU 301，¥1599.00）",
            ("sku_ids", [990, 301]),
            "router_task_20_catalog_search",
            id="spu-rank-compare-plus-cancel-order",
        ),
        pytest.param(
            "对比这个和 SKU 销量第二的版本，另外给这台键盘设置降价提醒",
            _sku_rank_compare_goal(),
            _human_handoff_goal("goal_23", "给这台键盘设置降价提醒", "price_alert"),
            600,
            [["task_22"], ["task_23"]],
            "Rank-2 Keyboard SKU（SKU 502，¥699.00）",
            ("sku_ids", [600, 502]),
            "router_task_22_catalog_search",
            id="sku-rank-compare-plus-price-alert",
        ),
        pytest.param(
            "查询最近订单详情，另外扫一下商品条形码告诉我 SKU",
            _order_detail_goal(),
            _human_handoff_goal("goal_25", "扫一下商品条形码告诉我 SKU", "barcode_scan"),
            None,
            [["task_24"], ["task_25"]],
            "订单 4321 当前状态为已发货",
            ("order_id", 4321),
            "router_task_24_order_lookup",
            id="order-detail-plus-barcode-scan",
        ),
    ],
)
async def test_e2e_chained_goal_completes_while_capability_gap_offers_handoff(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    admitted_goal: dict[str, Any],
    capability_gap_goal: dict[str, Any],
    referenced_sku_id: int | None,
    expected_waves: list[list[str]],
    expected_answer: str,
    expected_bound_argument: tuple[str, Any],
    internal_call_id: str,
) -> None:
    runtime, router, answer, executor, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query=message,
        goals=[admitted_goal, capability_gap_goal],
        referenced_sku_id=referenced_sku_id,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]

    assert len(state["route_plan"]["subqueries"]) == 2
    assert len(state["blocked_subqueries"]) == 1
    assert state["blocked_subqueries"][0]["disposition"] == "human_handoff"
    assert [
        [call["subquery"] for call in wave["calls"]] for wave in state["tool_waves"]
    ] == expected_waves
    assert len(executor.calls) == 2
    key, expected_value = expected_bound_argument
    if key == "sku_ids":
        actual_value = _runtime_catalog_sku_ids(executor.runtime_contexts[-1])
    else:
        actual_value = executor.calls[-1][1][key]
    assert actual_value == expected_value
    assert expected_answer in response.answer
    assert (
        "你的另一个需求当前智能客服无法满足，需要人工客服进一步处理。"
        "是否需要转人工客服？"
    ) in response.answer
    assert "另外：" not in response.answer
    assert response.boundary.classification == "human_handoff_required"
    assert response.suggested_actions[0].payload["handoff"] is True
    assert internal_call_id not in answer.last_used_tool_call_ids
    _assert_successful_e2e(
        state,
        router,
        answer,
        context,
        expected_boundary="human_handoff_required",
    )


def _rank_compare_goal(
    *,
    goal_number: int,
    rank_task_number: int,
    compare_task_number: int,
    category: str,
    scope: str,
    rank: int,
) -> dict[str, Any]:
    ordinal = {1: "一", 2: "二", 3: "三", 4: "四"}[rank]
    ranked_noun = "版本" if scope == "sku" else "商品"
    goal_id = f"goal_{goal_number}"
    rank_task_id = f"task_{rank_task_number}"
    compare_task_id = f"task_{compare_task_number}"
    return {
        "id": goal_id,
        "query": f"对比当前{category}和销量第{ordinal}的{category}",
        "disposition": "tool_planning",
        "reason_code": "compare_with_ranked_product",
        "tasks": [
            {
                "id": rank_task_id,
                "goal_id": goal_id,
                "canonical_query": (
                    f"查询当前商品以及{category} {scope.upper()} 销量排行第{ordinal}的"
                    f"{ranked_noun}，返回两项商品结果"
                ),
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "internal",
                "capability": "catalog_search",
            },
            {
                "id": compare_task_id,
                "goal_id": goal_id,
                "canonical_query": f"仅比较 {rank_task_id} 返回的两个{category}",
                "depends_on": [rank_task_id],
                "input_requirements": [],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }


def _single_goal_multitask_cases() -> list[Any]:
    order_goal = {
        "id": "goal_103",
        "query": "查询最近订单的完整详情",
        "disposition": "tool_planning",
        "reason_code": "latest_order_detail",
        "tasks": [
            {
                "id": "task_106",
                "goal_id": "goal_103",
                "canonical_query": "查询最近订单候选",
                "depends_on": [],
                "input_requirements": [],
                "produces": "order",
                "answer_role": "internal",
                "capability": "order_lookup",
            },
            {
                "id": "task_107",
                "goal_id": "goal_103",
                "canonical_query": "查询选中订单的详情",
                "depends_on": ["task_106"],
                "input_requirements": [
                    {
                        "name": "order_id",
                        "source": "task_output",
                        "task_id": "task_106",
                    }
                ],
                "produces": "order",
                "answer_role": "user_facing",
                "capability": "order_lookup",
            },
        ],
    }
    cross_category_compare = {
        "id": "goal_104",
        "query": "比较销量第一的键盘和销量第一的鼠标",
        "disposition": "tool_planning",
        "reason_code": "compare_two_ranked_products",
        "tasks": [
            {
                "id": "task_108",
                "goal_id": "goal_104",
                "canonical_query": "查询键盘 SPU 销量排行第一的商品",
                "depends_on": [],
                "input_requirements": [],
                "produces": "ranked_product",
                "answer_role": "internal",
                "capability": "catalog_search",
                "result_selector": {"type": "sales_rank", "rank": 1, "scope": "spu"},
            },
            {
                "id": "task_109",
                "goal_id": "goal_104",
                "canonical_query": "查询鼠标 SPU 销量排行第一的商品",
                "depends_on": [],
                "input_requirements": [],
                "produces": "ranked_product",
                "answer_role": "internal",
                "capability": "catalog_search",
                "result_selector": {"type": "sales_rank", "rank": 1, "scope": "spu"},
            },
            {
                "id": "task_110",
                "goal_id": "goal_104",
                "canonical_query": "比较键盘销量第一名与鼠标销量第一名",
                "depends_on": ["task_108", "task_109"],
                "input_requirements": [
                    {
                        "name": "keyboard",
                        "source": "task_output",
                        "task_id": "task_108",
                    },
                    {
                        "name": "mouse",
                        "source": "task_output",
                        "task_id": "task_109",
                    },
                ],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }
    recommendation_policy = {
        "id": "goal_105",
        "query": "推荐一个无线鼠标并说明是否支持七天退货",
        "disposition": "tool_planning",
        "reason_code": "recommend_with_return_policy",
        "tasks": [
            {
                "id": "task_111",
                "goal_id": "goal_105",
                "canonical_query": "推荐一个无线鼠标",
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "user_facing",
                "capability": "catalog_search",
            },
            {
                "id": "task_112",
                "goal_id": "goal_105",
                "canonical_query": "查询商城七天退货政策",
                "depends_on": [],
                "input_requirements": [],
                "produces": "documents",
                "answer_role": "user_facing",
                "capability": "policy_search",
            },
        ],
    }
    recommendation_knowledge = {
        "id": "goal_106",
        "query": "推荐一个键盘并说明选购机械轴时要注意什么",
        "disposition": "tool_planning",
        "reason_code": "recommend_with_buying_knowledge",
        "tasks": [
            {
                "id": "task_113",
                "goal_id": "goal_106",
                "canonical_query": "推荐一个键盘",
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "user_facing",
                "capability": "catalog_search",
            },
            {
                "id": "task_114",
                "goal_id": "goal_106",
                "canonical_query": "查询机械键盘轴体选购知识",
                "depends_on": [],
                "input_requirements": [],
                "produces": "documents",
                "answer_role": "user_facing",
                "capability": "knowledge_search",
            },
        ],
    }
    facets_recommendation = {
        "id": "goal_107",
        "query": "查看显示器可选品牌并推荐一个显示器",
        "disposition": "tool_planning",
        "reason_code": "inspect_brands_and_recommend",
        "tasks": [
            {
                "id": "task_115",
                "goal_id": "goal_107",
                "canonical_query": "查询显示器有哪些品牌",
                "depends_on": [],
                "input_requirements": [],
                "produces": "facets",
                "answer_role": "user_facing",
                "capability": "catalog_facets",
            },
            {
                "id": "task_116",
                "goal_id": "goal_107",
                "canonical_query": "推荐一个显示器",
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "user_facing",
                "capability": "catalog_search",
            },
        ],
    }
    order_policy = {
        "id": "goal_108",
        "query": "查询最近订单详情并说明这笔订单适用的退货政策",
        "disposition": "tool_planning",
        "reason_code": "order_detail_with_return_policy",
        "tasks": [
            {
                "id": "task_117",
                "goal_id": "goal_108",
                "canonical_query": "查询最近订单候选",
                "depends_on": [],
                "input_requirements": [],
                "produces": "order",
                "answer_role": "internal",
                "capability": "order_lookup",
            },
            {
                "id": "task_118",
                "goal_id": "goal_108",
                "canonical_query": "查询选中订单的详情",
                "depends_on": ["task_117"],
                "input_requirements": [
                    {
                        "name": "order_id",
                        "source": "task_output",
                        "task_id": "task_117",
                    }
                ],
                "produces": "order",
                "answer_role": "user_facing",
                "capability": "order_lookup",
            },
            {
                "id": "task_119",
                "goal_id": "goal_108",
                "canonical_query": "查询商城退货政策",
                "depends_on": [],
                "input_requirements": [],
                "produces": "documents",
                "answer_role": "user_facing",
                "capability": "policy_search",
            },
        ],
    }
    headset_compare_knowledge = {
        "id": "goal_109",
        "query": "对比当前耳机和销量第二的耳机并说明耳机选购要点",
        "disposition": "tool_planning",
        "reason_code": "compare_headset_with_knowledge",
        "tasks": [
            {
                "id": "task_120",
                "goal_id": "goal_109",
                "canonical_query": (
                    "查询当前商品以及耳机 SPU 销量排行第二的商品，"
                    "返回两项商品结果"
                ),
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "internal",
                "capability": "catalog_search",
            },
            {
                "id": "task_121",
                "goal_id": "goal_109",
                "canonical_query": "仅比较 task_120 返回的两个耳机",
                "depends_on": ["task_120"],
                "input_requirements": [],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
            {
                "id": "task_122",
                "goal_id": "goal_109",
                "canonical_query": "查询耳机选购知识",
                "depends_on": [],
                "input_requirements": [],
                "produces": "documents",
                "answer_role": "user_facing",
                "capability": "knowledge_search",
            },
        ],
    }
    return [
        pytest.param(
            "对比这个和销量第三的显示器",
            _rank_compare_goal(
                goal_number=100,
                rank_task_number=100,
                compare_task_number=101,
                category="显示器",
                scope="spu",
                rank=3,
            ),
            990,
            [[("task_100", "catalog_search")], [("task_101", "catalog_compare")]],
            ["Rank-3 Monitor（SKU 301，¥1599.00）"],
            0,
            id="current-monitor-vs-spu-rank-3",
        ),
        pytest.param(
            "对比这个和 SKU 销量第二的键盘版本",
            _rank_compare_goal(
                goal_number=101,
                rank_task_number=102,
                compare_task_number=103,
                category="键盘",
                scope="sku",
                rank=2,
            ),
            600,
            [[("task_102", "catalog_search")], [("task_103", "catalog_compare")]],
            ["Rank-2 Keyboard SKU（SKU 502，¥699.00）"],
            0,
            id="current-keyboard-vs-sku-rank-2",
        ),
        pytest.param(
            "对比这个和销量第一的鼠标",
            _rank_compare_goal(
                goal_number=102,
                rank_task_number=104,
                compare_task_number=105,
                category="鼠标",
                scope="spu",
                rank=1,
            ),
            700,
            [[("task_104", "catalog_search")], [("task_105", "catalog_compare")]],
            ["Rank-1 Mouse（SKU 710，¥329.00）"],
            0,
            id="current-mouse-vs-spu-rank-1",
        ),
        pytest.param(
            "查询最近订单的完整详情",
            order_goal,
            None,
            [[("task_106", "order_lookup")], [("task_107", "order_lookup")]],
            ["订单 4321 当前状态为已发货"],
            0,
            id="latest-order-candidate-then-detail",
        ),
        pytest.param(
            "比较销量第一的键盘和销量第一的鼠标",
            cross_category_compare,
            None,
            [
                [("task_108", "catalog_search"), ("task_109", "catalog_search")],
                [("task_110", "catalog_compare")],
            ],
            ["Rank-1 Keyboard SKU", "Rank-1 Mouse"],
            0,
            id="two-ranked-products-then-compare",
        ),
        pytest.param(
            "推荐一个无线鼠标并说明是否支持七天退货",
            recommendation_policy,
            None,
            [[("task_111", "catalog_search"), ("task_112", "policy_search")]],
            ["Mouse Choice（SKU 901，¥299.00）", "七天内申请退货"],
            0,
            id="recommendation-and-policy-in-parallel",
        ),
        pytest.param(
            "推荐一个键盘并说明选购机械轴时要注意什么",
            recommendation_knowledge,
            None,
            [[("task_113", "catalog_search"), ("task_114", "knowledge_search")]],
            ["Keyboard Choice（SKU 401，¥499.00）", "连接方式、尺寸和使用场景"],
            0,
            id="recommendation-and-knowledge-in-parallel",
        ),
        pytest.param(
            "查看显示器可选品牌并推荐一个显示器",
            facets_recommendation,
            None,
            [[("task_115", "catalog_facets"), ("task_116", "catalog_search")]],
            ["E2E Display、Fixture Vision", "Rank-3 Monitor（SKU 301，¥1599.00）"],
            0,
            id="facets-and-recommendation-in-parallel",
        ),
        pytest.param(
            "查询最近订单详情并说明这笔订单适用的退货政策",
            order_policy,
            None,
            [
                [("task_117", "order_lookup"), ("task_119", "policy_search")],
                [("task_118", "order_lookup")],
            ],
            ["订单 4321 当前状态为已发货", "七天内申请退货"],
            0,
            id="order-detail-chain-with-parallel-policy",
        ),
        pytest.param(
            "对比当前耳机和销量第二的耳机并说明耳机选购要点",
            headset_compare_knowledge,
            800,
            [
                [("task_120", "catalog_search"), ("task_122", "knowledge_search")],
                [("task_121", "catalog_compare")],
            ],
            ["Rank-2 Headset（SKU 802，¥599.00）", "连接方式、尺寸和使用场景"],
            0,
            id="ranked-headset-compare-with-parallel-knowledge",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "message",
        "goal",
        "referenced_sku_id",
        "expected_waves",
        "expected_outputs",
        "expected_planner_calls",
    ),
    _single_goal_multitask_cases(),
)
async def test_e2e_single_goal_expands_to_multiple_tasks(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    goal: dict[str, Any],
    referenced_sku_id: int | None,
    expected_waves: list[list[tuple[str, str]]],
    expected_outputs: list[str],
    expected_planner_calls: int,
) -> None:
    runtime, router, answer, _, context = _runtime(
        monkeypatch,
        message=message,
        rewritten_query=message,
        goals=[goal],
        referenced_sku_id=referenced_sku_id,
    )

    response = await runtime.run(ChatRequest(message=message), user_id=7)
    state = context.outcomes[0]
    route_goals = state["route_plan"]["subqueries"]
    actual_waves = [
        [(call["subquery"], call["name"]) for call in wave["calls"]] for wave in state["tool_waves"]
    ]

    assert len(route_goals) == 1
    assert len(route_goals[0]["tasks"]) >= 2
    assert actual_waves == expected_waves
    assert {task["id"]: task["capability"] for task in route_goals[0]["tasks"]} == {
        task_id: tool_name for wave in expected_waves for task_id, tool_name in wave
    }
    for wave in state["tool_waves"]:
        for call in wave["calls"]:
            task = next(item for item in route_goals[0]["tasks"] if item["id"] == call["subquery"])
            assert call["canonical_query"] == task["canonical_query"]
            assert call["arguments"].get("query") == task["canonical_query"]
    for expected_output in expected_outputs:
        assert expected_output in response.answer
    _assert_successful_e2e(
        state,
        router,
        answer,
        context,
        expected_planner_calls=expected_planner_calls,
        expected_tool_waves=len(expected_waves),
    )


class _RecordingRegistryExecutor:
    def __init__(self, session: AsyncSession, settings: Settings):
        self.delegate = RegistryToolExecutor(session, settings)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.runtime_contexts: list[dict[str, Any]] = []

    async def execute(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> ToolExecutionResult:
        self.calls.append((contract.registry_name, dict(arguments)))
        self.runtime_contexts.append(dict(runtime_context))
        return await self.delegate.execute(contract, arguments, runtime_context)


async def _top_two_mouse_spus(session: AsyncSession) -> tuple[Spu, Spu]:
    spus = (
        await session.execute(
            select(Spu)
            .join(Category, Spu.category_id == Category.id)
            .where(
                Category.name.in_(["mouse", "鼠标"]),
                Spu.status == 1,
            )
            .order_by(Spu.sales_count.desc(), Spu.id)
            .limit(2)
        )
    ).scalars().all()
    assert len(spus) == 2
    return spus[0], spus[1]


async def _first_active_sku(session: AsyncSession, spu_id: int) -> Sku:
    sku = (
        await session.execute(
            select(Sku)
            .where(Sku.spu_id == spu_id, Sku.status == 1)
            .order_by(Sku.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    assert sku is not None
    return sku


def _ranked_spu_compare_goal() -> dict[str, Any]:
    return {
        "id": "goal_201",
        "query": "对比当前鼠标系列和销量第二的鼠标系列",
        "disposition": "tool_planning",
        "reason_code": "compare_current_with_second_series",
        "tasks": [
            {
                "id": "task_201",
                "goal_id": "goal_201",
                "canonical_query": (
                    "查询当前商品以及鼠标 SPU 销量排行第二的商品，"
                    "返回两项商品结果"
                ),
                "depends_on": [],
                "input_requirements": [],
                "produces": "products",
                "answer_role": "internal",
                "capability": "catalog_search",
            },
            {
                "id": "task_202",
                "goal_id": "goal_201",
                "canonical_query": "仅比较 task_201 返回的两个鼠标系列",
                "depends_on": ["task_201"],
                "input_requirements": [],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            },
        ],
    }


def _depends_on_only_top_two_mouse_compare_goal() -> dict[str, Any]:
    query = "把销量前两名游戏鼠标的重量和 DPI 对比一下"
    return {
        "id": "goal_204",
        "query": query,
        "disposition": "tool_planning",
        "reason_code": "rank_then_compare",
        "tasks": [
            {
                "id": "task_204",
                "goal_id": "goal_204",
                "canonical_query": "找出销量排名前两名的游戏鼠标",
                "depends_on": [],
                "input_requirements": [],
                "produces": "result",
                "answer_role": "internal",
                "capability": "planner_required",
            },
            {
                "id": "task_205",
                "goal_id": "goal_204",
                "canonical_query": query,
                "depends_on": ["task_204"],
                "input_requirements": [],
                "produces": "result",
                "answer_role": "user_facing",
                "capability": "planner_required",
            },
        ],
    }


def _depends_on_only_current_and_ranked_mouse_compare_goal() -> dict[str, Any]:
    query = "和销量第二的鼠标系列对比一下"
    return {
        "id": "goal_206",
        "query": query,
        "disposition": "tool_planning",
        "reason_code": "compare_memory_product_with_ranked",
        "tasks": [
            {
                "id": "task_206",
                "goal_id": "goal_206",
                "canonical_query": (
                    "查询当前商品以及鼠标 SPU 销量排行第二的商品，返回两项商品结果"
                ),
                "depends_on": [],
                "input_requirements": [],
                "produces": "result",
                "answer_role": "internal",
                "capability": "planner_required",
            },
            {
                "id": "task_207",
                "goal_id": "goal_206",
                "canonical_query": "仅比较 task_206 返回的两个商品系列",
                "depends_on": ["task_206"],
                "input_requirements": [],
                "produces": "result",
                "answer_role": "user_facing",
                "capability": "planner_required",
            },
        ],
    }


def _comparison_context_goal(*, level: str) -> dict[str, Any]:
    canonical_query = (
        "重新比较刚才两个具体 SKU 版本的当前目录事实"
        if level == "sku"
        else "重新比较刚才两个商品系列的当前目录事实"
    )
    return {
        "id": "goal_203",
        "query": "继续比较刚才两个商品",
        "disposition": "tool_planning",
        "reason_code": "refresh_previous_comparison",
        "tasks": [
            {
                "id": "task_203",
                "goal_id": "goal_203",
                "canonical_query": canonical_query,
                "depends_on": [],
                "input_requirements": [
                    {
                        "name": "comparison_products",
                        "source": "comparison_context",
                    }
                ],
                "produces": "comparison",
                "answer_role": "user_facing",
                "capability": "catalog_compare",
            }
        ],
    }


def _real_catalog_runtime(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    *,
    message: str,
    goal: dict[str, Any],
    working_memory: WorkingMemoryV2,
    answer_model: _ArtifactAnswerModel | None = None,
) -> tuple[
    AgentRuntime,
    _RouterModel,
    _ArtifactAnswerModel,
    _RecordingRegistryExecutor,
    _ContextService,
]:
    monkeypatch.setattr("app.agent.graph.ConversationRepository", _AuditRepository)
    settings = Settings(llm_api_key="", catalog_llm_planner_enabled=False)
    router = _RouterModel(_route_message(message, [goal]))
    answer = answer_model or _ArtifactAnswerModel()
    executor = _RecordingRegistryExecutor(session, settings)
    context = _ContextService(message, working_memory=working_memory)
    runtime = AgentRuntime(
        session,
        settings,
        chat_model=answer,
        router_model=router,
        context_service=context,
        tool_executor=executor,
    )
    return runtime, router, answer, executor, context


@pytest.mark.asyncio
async def test_e2e_depends_on_only_top_two_mouse_search_flows_into_real_compare(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory,
) -> None:
    message = "把销量前两名游戏鼠标的重量和 DPI 对比一下"
    async with db_session_factory() as session:
        runtime, router, answer, executor, context = _real_catalog_runtime(
            monkeypatch,
            session,
            message=message,
            goal=_depends_on_only_top_two_mouse_compare_goal(),
            working_memory=WorkingMemoryV2(),
            answer_model=_AutonomousArtifactModel(),
        )

        response = await runtime.run(ChatRequest(message=message), user_id=7)

    state = context.outcomes[0]
    assert [
        [(call["subquery"], call["name"]) for call in wave["calls"]]
        for wave in state["tool_waves"]
    ] == [
        [("task_204", "catalog_search")],
        [("task_205", "catalog_compare")],
    ]
    assert state["route_plan"]["subqueries"][0]["tasks"][1]["input_requirements"] == []
    search_artifact = state["task_artifacts"]["task_204"]["value"]
    assert search_artifact["returned_count"] == 2
    assert len(search_artifact["products"]) == 2
    compare_context = executor.runtime_contexts[-1]["catalog_context"]
    assert compare_context["dependency_artifacts"][0]["task_id"] == "task_204"
    assert len(compare_context["dependency_artifacts"][0]["products"]) == 2
    comparison = state["task_artifacts"]["task_205"]["value"]
    compared_count = len(comparison["products"]) or len(comparison["series"])
    assert compared_count == 2
    assert state["task_status"]["task_205"]["status"] == "succeeded"
    assert "没有找到" not in response.answer
    _assert_successful_e2e(
        state,
        router,
        answer,
        context,
        expected_planner_calls=2,
    )


@pytest.mark.asyncio
async def test_e2e_current_memory_plus_ranked_artifact_compare_survives_privacy_refusal(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory,
) -> None:
    message = "和销量第二对比一下呢？有谁买了这些产品"
    privacy_goal = {
        "id": "goal_207",
        "query": "查询购买这些商品的其他客户信息",
        "disposition": "security_refusal",
        "reason_code": "other_customer_purchase_data",
    }
    async with db_session_factory() as session:
        current_spu, ranked_spu = await _top_two_mouse_spus(session)
        current_sku = await _first_active_sku(session, current_spu.id)
        working_memory = WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "query_plan": {"selection_scope": "spu"},
                    "candidate_spu_ids": [current_spu.id],
                    "candidate_sku_ids": [current_sku.id],
                    "candidate_display": [
                        {
                            "spu_id": current_spu.id,
                            "sku_id": current_sku.id,
                            "title": current_spu.title,
                            "entity_scope": "spu",
                        }
                    ],
                }
            }
        )
        monkeypatch.setattr("app.agent.graph.ConversationRepository", _AuditRepository)
        settings = Settings(llm_api_key="", catalog_llm_planner_enabled=False)
        router = _RouterModel(
            _route_message(
                message,
                [
                    _depends_on_only_current_and_ranked_mouse_compare_goal(),
                    privacy_goal,
                ],
            )
        )
        answer = _AutonomousArtifactModel()
        executor = _RecordingRegistryExecutor(session, settings)
        context = _ContextService(message, working_memory=working_memory)
        runtime = AgentRuntime(
            session,
            settings,
            chat_model=answer,
            router_model=router,
            context_service=context,
            tool_executor=executor,
        )

        response = await runtime.run(ChatRequest(message=message), user_id=7)

    state = context.outcomes[0]
    assert [
        [(call["subquery"], call["name"]) for call in wave["calls"]]
        for wave in state["tool_waves"]
    ] == [
        [("task_206", "catalog_search")],
        [("task_207", "catalog_compare")],
    ]
    search_artifact = state["task_artifacts"]["task_206"]["value"]
    assert search_artifact["selected_spu_ids"] == [current_spu.id, ranked_spu.id]
    assert len(search_artifact["products"]) == 2
    compare_context = executor.runtime_contexts[-1]["catalog_context"]
    assert compare_context["memory"] == {}
    assert compare_context["dependency_artifacts"][0]["selected_spu_ids"] == [
        current_spu.id,
        ranked_spu.id
    ]
    comparison = state["task_artifacts"]["task_207"]["value"]
    assert comparison["selected_spu_ids"] == [current_spu.id, ranked_spu.id]
    assert current_spu.title in response.answer
    assert ranked_spu.title in response.answer
    assert "不能查询或披露其他客户的信息" in response.answer
    assert "没有找到" not in response.answer
    assert state["task_status"]["task_207"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_e2e_ranked_spu_compare_aggregates_active_variants_with_real_catalog(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory,
) -> None:
    message = "这个鼠标系列和销量第二的鼠标系列有什么区别"
    async with db_session_factory() as session:
        current_spu, ranked_spu = await _top_two_mouse_spus(session)
        current_sku = await _first_active_sku(session, current_spu.id)
        ranked_sku = await _first_active_sku(session, ranked_spu.id)
        active_marker = "E2E active Bluetooth variant"
        inactive_marker = "E2E inactive variant"
        session.add_all(
            [
                Sku(
                    spu_id=current_spu.id,
                    title=active_marker,
                    price=current_sku.price,
                    stock=4,
                    sales_count=0,
                    specs_json={"connection_type": "Bluetooth", "e2e_marker": "active"},
                    status=1,
                ),
                Sku(
                    spu_id=current_spu.id,
                    title=inactive_marker,
                    price=current_sku.price,
                    stock=99,
                    sales_count=0,
                    specs_json={"connection_type": "Inactive-only"},
                    status=0,
                ),
                Sku(
                    spu_id=ranked_spu.id,
                    title="E2E ranked wired variant",
                    price=ranked_sku.price,
                    stock=2,
                    sales_count=0,
                    specs_json={"connection_type": "Wired", "e2e_marker": "ranked"},
                    status=1,
                ),
            ]
        )
        await session.flush()
        expected_active_counts = dict(
            (
                await session.execute(
                    select(Sku.spu_id, func.count(Sku.id))
                    .where(
                        Sku.spu_id.in_([current_spu.id, ranked_spu.id]),
                        Sku.status == 1,
                    )
                    .group_by(Sku.spu_id)
                )
            ).all()
        )
        working_memory = WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "referenced_sku_id": current_sku.id,
                    "candidate_sku_ids": [current_sku.id],
                }
            }
        )
        runtime, router, answer, executor, context = _real_catalog_runtime(
            monkeypatch,
            session,
            message=message,
            goal=_ranked_spu_compare_goal(),
            working_memory=working_memory,
        )

        response = await runtime.run(ChatRequest(message=message), user_id=7)

    state = context.outcomes[0]
    assert [
        [(call["subquery"], call["name"]) for call in wave["calls"]]
        for wave in state["tool_waves"]
    ] == [
        [("task_201", "catalog_search")],
        [("task_202", "catalog_compare")],
    ]
    compare_name, compare_arguments = executor.calls[-1]
    assert compare_name == "catalog.compare"
    assert compare_arguments == {
        "query": "仅比较 task_201 返回的两个鼠标系列",
        "limit": 5,
    }
    catalog_context = executor.runtime_contexts[-1]["catalog_context"]
    assert catalog_context["memory"] == {}
    assert len(catalog_context["dependency_artifacts"]) == 1
    ranked_dependency = catalog_context["dependency_artifacts"][0]
    assert ranked_dependency["selected_spu_ids"] == [
        current_spu.id,
        ranked_spu.id,
    ]
    ranked_artifact = state["task_artifacts"]["task_201"]["value"]
    assert ranked_artifact["selected_spu_ids"] == [
        current_spu.id,
        ranked_spu.id,
    ]
    assert ranked_artifact["selected_sku_ids"] == ranked_dependency["selected_sku_ids"]
    assert ranked_spu.id not in working_memory.catalog.candidate_spu_ids
    comparison_output = state["tool_results"][-1]["execution"]["output"]
    assert comparison_output["comparison_level"] == "spu"
    assert comparison_output["products"] == []
    assert [item["spu_id"] for item in comparison_output["series"]] == [
        current_spu.id,
        ranked_spu.id,
    ]
    assert {
        item["spu_id"]: item["sku_count"] for item in comparison_output["series"]
    } == expected_active_counts
    current_series = comparison_output["series"][0]
    variant_titles = {item["title"] for item in current_series["variants"]}
    assert active_marker in variant_titles
    assert inactive_marker not in variant_titles
    assert current_spu.title in response.answer
    assert ranked_spu.title in response.answer
    _assert_successful_e2e(state, router, answer, context)


@pytest.mark.asyncio
async def test_e2e_spu_comparison_context_refresh_uses_one_real_tool_wave(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory,
) -> None:
    message = "继续比较刚才两个系列的连接方式"
    async with db_session_factory() as session:
        left, right = await _top_two_mouse_spus(session)
        working_memory = WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "comparison": {
                        "query": "上一轮系列比较",
                        "comparison_level": "spu",
                        "spu_ids": [left.id, right.id],
                    }
                }
            }
        )
        runtime, router, answer, executor, context = _real_catalog_runtime(
            monkeypatch,
            session,
            message=message,
            goal=_comparison_context_goal(level="spu"),
            working_memory=working_memory,
        )

        response = await runtime.run(ChatRequest(message=message), user_id=7)

    state = context.outcomes[0]
    assert len(state["tool_waves"]) == 1
    assert executor.calls == [
        (
            "catalog.compare",
            {
                "query": "重新比较刚才两个商品系列的当前目录事实",
                "limit": 5,
            },
        )
    ]
    assert executor.runtime_contexts[0]["user_id"] == 7
    catalog_context = executor.runtime_contexts[0]["catalog_context"]
    assert catalog_context["memory"]["comparison"]["comparison_level"] == "spu"
    assert catalog_context["memory"]["comparison"]["spu_ids"] == [
        left.id,
        right.id,
    ]
    assert catalog_context["dependency_artifacts"] == []
    comparison = state["task_artifacts"]["task_203"]["value"]
    assert comparison["comparison_level"] == "spu"
    assert comparison["selected_spu_ids"] == [left.id, right.id]
    assert left.title in response.answer
    assert right.title in response.answer
    _assert_successful_e2e(
        state,
        router,
        answer,
        context,
        expected_tool_waves=1,
    )


@pytest.mark.asyncio
async def test_e2e_explicit_sku_comparison_keeps_legacy_contract_with_real_catalog(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory,
) -> None:
    message = "重新比较刚才两个具体版本"
    async with db_session_factory() as session:
        left_spu, right_spu = await _top_two_mouse_spus(session)
        left_sku = await _first_active_sku(session, left_spu.id)
        right_sku = await _first_active_sku(session, right_spu.id)
        working_memory = WorkingMemoryV2.model_validate(
            {
                "catalog": {
                    "comparison": {
                        "query": "上一轮 SKU 比较",
                        "comparison_level": "sku",
                        "sku_ids": [left_sku.id, right_sku.id],
                    }
                }
            }
        )
        runtime, router, answer, executor, context = _real_catalog_runtime(
            monkeypatch,
            session,
            message=message,
            goal=_comparison_context_goal(level="sku"),
            working_memory=working_memory,
        )

        response = await runtime.run(ChatRequest(message=message), user_id=7)

    state = context.outcomes[0]
    assert executor.calls == [
        (
            "catalog.compare",
            {
                "query": "重新比较刚才两个具体 SKU 版本的当前目录事实",
                "limit": 5,
            },
        )
    ]
    assert executor.runtime_contexts[0]["user_id"] == 7
    catalog_context = executor.runtime_contexts[0]["catalog_context"]
    assert catalog_context["memory"]["comparison"]["comparison_level"] == "sku"
    assert catalog_context["memory"]["comparison"]["sku_ids"] == [
        left_sku.id,
        right_sku.id,
    ]
    assert catalog_context["dependency_artifacts"] == []
    comparison_output = state["tool_results"][0]["execution"]["output"]
    assert comparison_output["comparison_level"] == "sku"
    assert comparison_output["series"] == []
    assert [item["sku_id"] for item in comparison_output["products"]] == [
        left_sku.id,
        right_sku.id,
    ]
    assert left_sku.title in response.answer
    assert right_sku.title in response.answer
    _assert_successful_e2e(
        state,
        router,
        answer,
        context,
        expected_tool_waves=1,
    )
