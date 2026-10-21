import json
from datetime import datetime
from decimal import Decimal

import app.tools.orders as order_tools
import pytest
from app.core.config import Settings
from app.repositories.orders import OrderSearchPage
from app.schemas.order import LogisticsCard, OrderCard, OrderItemCard
from app.tools.contracts import (
    BoundTool,
    DefaultToolContractProvider,
    build_order_planner,
    execute_bound_tool,
)
from app.tools.schemas import (
    OrderLookupInput,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.tool_plan_fixtures import order_plan
from tests.support.tool_planning import scripted_review

TOOL_TEST_SETTINGS = Settings(llm_api_key="", catalog_llm_planner_enabled=False)


class FakeChatResponse:
    def __init__(self, content: str):
        self.content = content


class FakeChatModel:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def ainvoke(self, messages):
        if response := scripted_review(messages):
            self.review_calls = [*getattr(self, "review_calls", []), list(messages)]
            return response
        self.calls.append(messages)
        return FakeChatResponse(self.content)


def test_build_order_planner_uses_llm_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = FakeChatModel('{"selection":"all"}')
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings, **kwargs: chat)

    planner = build_order_planner(Settings(llm_api_key="test"))

    assert isinstance(planner, order_tools.LLMOrderQueryPlanner)
    assert planner.chat_model is chat


def test_build_order_planner_skips_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings, **kwargs: None)

    planner = build_order_planner(Settings(llm_api_key=""))

    assert planner is None


@pytest.mark.parametrize(
    ("query", "expected_selection", "expected_operation", "expected_shape", "expected_limit"),
    [
        ("我到底有多少订单呀？", "all", "total_count", "count", 5),
        ("我有几个订单？最近的列出来让我选", "recent", "list", "summary", 5),
        ("最近三笔订单给我简单列一下", "recent", "list", "summary", 5),
        ("我最近一笔订单是什么", "latest", "detail", "single_detail", 1),
        ("最新那单物流到哪儿了", "latest", "detail", "single_detail", 1),
        ("所有订单都给我列出来", "all", "list", "summary", 5),
        ("我是不是买过雷蛇鼠标，在哪个订单来着？", "all", "match_orders", "analysis_detail", 5),
        ("桌面音响我什么时候买的？", "all", "match_orders", "analysis_detail", 5),
        ("哪些订单已经发货", "all", "match_orders", "analysis_detail", 5),
        ("去年买过什么外设", "all", "match_orders", "analysis_detail", 5),
        ("所有订单的商品明细都摊开给我看看", "all", "detail", "analysis_detail", 5),
        ("我每一单都寄到哪里了，帮我弄个地址表", "all", "list", "unsupported", 5),
    ],
)
def test_order_fast_path_delegates_open_language_without_losing_constraints(
    query: str,
    expected_selection: str,
    expected_operation: str,
    expected_shape: str,
    expected_limit: int,
) -> None:
    request = OrderLookupInput(user_id=1, query=query)
    plan = order_tools._deterministic_fast_plan(request, order_id=None)

    assert plan is None


@pytest.mark.parametrize(
    "query",
    [
        "桌面音响我什么时候买的？",
        "那个桌面音箱是啥时候买的",
        "我哪天买的 speaker？",
        "查询桌面音响的购买时间",
    ],
)
def test_order_purchase_time_requires_a_complete_product_filter_plan(
    query: str,
) -> None:
    request = OrderLookupInput(user_id=1, query=query)

    plan = order_tools._deterministic_fast_plan(request, order_id=None)

    assert plan is None


def test_order_llm_prompt_maps_product_purchase_time_without_inventing_field() -> None:
    prompt = order_tools._order_planner_system_prompt()

    assert '"桌面音响我什么时候买的"' in prompt
    assert "purchase_history_match" in prompt
    assert "created_at" in prompt
    assert "Do not invent purchase_date" in prompt


def test_order_fast_path_accepts_plain_english_explicit_order_id() -> None:
    request = OrderLookupInput(
        user_id=1,
        query="please check order 202607020001",
    )
    order_id = order_tools._extract_order_id(request.query)

    plan = order_tools._deterministic_fast_plan(request, order_id=order_id)

    assert order_id == 202607020001
    assert plan is not None
    assert plan.selection == "explicit"
    assert order_tools.validate_order_query_plan(
        plan,
        order_id=order_id,
    ).result_shape == "single_detail"


@pytest.mark.asyncio
async def test_llm_order_planner_catches_colloquial_supported_request() -> None:
    planner = order_tools.LLMOrderQueryPlanner(
        FakeChatModel(
            """
            {
              "selection": "all",
              "operation": "match_orders",
              "requested_fields": ["purchase_history_match"],
              "filters": {"time_field":"created_at","start_at":null,"end_at":null,
                          "statuses":[],"product_terms":["雷蛇","耳机"]},
              "filter_evidence": ["雷蛇耳机"], "unresolved_requirements":[],
              "limit": 5
            }
            """
        )
    )

    plan = await planner.plan(
        OrderLookupInput(
            user_id=1,
            query="我头前买的那副雷蛇耳机到底压在哪张单子里，帮我翻出来",
        )
    )
    validated = order_tools.validate_order_query_plan(plan, order_id=None)

    assert plan.planner == "llm"
    assert plan.operation == "match_orders"
    assert validated.result_shape == "analysis_detail"
    assert "order_items" in validated.supported_fields


@pytest.mark.asyncio
async def test_llm_order_planner_preserves_unusual_unavailable_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RepositoryMustNotBeCreated:
        def __init__(self, session: AsyncSession):
            raise AssertionError("unsupported query must not reach the repository")

    monkeypatch.setattr(order_tools, "OrderRepository", RepositoryMustNotBeCreated)
    planner = order_tools.LLMOrderQueryPlanner(
        FakeChatModel(
            """
            {
              "selection": "all",
              "operation": "detail",
              "requested_fields": ["buyer_message"],
              "filters": {"time_field":"created_at","start_at":null,"end_at":null,
                          "statuses":[],"product_terms":[]},
              "filter_evidence":[],"unresolved_requirements":[],
              "limit": 5
            }
            """
        )
    )
    service = order_tools.OrderToolService(None, planner=planner)  # type: ignore[arg-type]

    result = await service.lookup(
        OrderLookupInput(
            user_id=1,
            query="我下单时好像给商家嘱咐过一句话，能把当时写的内容翻出来吗？",
        )
    )

    assert result.result_type == "unsupported"
    assert result.diagnostics[0].code == "unsupported_query"
    assert result.query_plan["unsupported_fields"] == ["buyer_message"]


@pytest.mark.asyncio
async def test_llm_order_planner_maps_indirect_arrival_prediction_to_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RepositoryMustNotBeCreated:
        def __init__(self, session: AsyncSession):
            raise AssertionError("LLM-classified ETA request must not reach the repository")

    monkeypatch.setattr(order_tools, "OrderRepository", RepositoryMustNotBeCreated)
    chat = FakeChatModel(
        """
        {
          "selection": "explicit",
          "operation": "detail",
          "requested_fields": ["estimated_delivery_time"],
              "filters": {"time_field":"created_at","start_at":null,"end_at":null,
                          "statuses":[],"product_terms":[]},
              "filter_evidence":[],"unresolved_requirements":[],
          "limit": 1
        }
        """
    )
    planner = order_tools.LLMOrderQueryPlanner(chat)
    service = order_tools.OrderToolService(
        None,  # type: ignore[arg-type]
        planner=planner,
    )
    request = OrderLookupInput(
        user_id=1,
        order_id=991000000103,
        query="照眼下这个走法，我周末前能收着不？",
    )

    assert order_tools._deterministic_fast_plan(
        request,
        order_id=request.order_id,
    ) is None
    result = await service.lookup(request)

    assert len(chat.calls) == 1
    system_prompt = chat.calls[0][0].content
    assert "周末前能收到不" in system_prompt
    assert "Do not reinterpret" in system_prompt
    assert result.result_type == "unsupported"
    assert result.diagnostics[0].code == "unsupported_query"
    assert result.query_plan["unsupported_fields"] == ["estimated_delivery_time"]


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_mixed_single_order_request_queries_supported_fields_and_reports_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[int, int]] = []
    card = OrderCard(
        id=202607020001,
        status=3,
        status_label="已发货",
        total_amount=Decimal("109.00"),
        pay_amount=Decimal("109.00"),
        freight_amount=Decimal("10.00"),
        created_at=datetime(2026, 7, 20, 12, 0),
        items=[],
        logistics=None,
    )

    class FakeOrderRepository:
        def __init__(self, session: AsyncSession):
            self.session = session

        async def get_order(self, user_id: int, order_id: int) -> OrderCard | None:
            requested.append((user_id, order_id))
            return card

    monkeypatch.setattr(order_tools, "OrderRepository", FakeOrderRepository)
    service = order_tools.OrderToolService(None, planner=order_tools.LLMOrderQueryPlanner(
        FakeChatModel(json.dumps(order_plan({
            "selection": "explicit", "operation": "detail",
            "requested_fields": ["order_status", "payment_method"],
        }), ensure_ascii=False))
    ))

    result = await service.lookup(
        OrderLookupInput(
            user_id=7,
            query="帮我瞅下订单 202607020001 现在啥状态，当时又是拿什么方式付的？",
        )
    )

    assert requested == [(7, 202607020001)]
    assert result.result_type == "single_order"
    assert result.order == card
    assert result.diagnostics[0].code == "partial_unsupported_query"
    assert result.query_plan["supported_fields"] == ["order_status"]
    assert result.query_plan["unsupported_fields"] == ["payment_method"]


def test_order_summary_contains_safe_amount_and_latest_shipping_snapshot() -> None:
    card = OrderCard(
        id=202607020001,
        status=3,
        status_label="已发货",
        total_amount=Decimal("109.00"),
        pay_amount=Decimal("109.00"),
        freight_amount=Decimal("10.00"),
        created_at=datetime(2026, 7, 20, 12, 0),
        items=[
            OrderItemCard(
                id=1,
                sku_id=101,
                sku_name="雷蛇无线鼠标",
                sku_specs={"连接": "无线"},
                price=Decimal("99.00"),
                quantity=1,
            )
        ],
        logistics=LogisticsCard(
            express_company="顺丰速运",
            logistic_no="SF100200300400",
            status=2,
            trace=[
                {"time": "2026-07-20 15:20", "text": "快件已揽收"},
                {
                    "time": "2026-07-21 09:10",
                    "text": "快件正在发往上海转运中心",
                },
            ],
        ),
    )

    summary = order_tools._summary(card)

    assert summary.total_amount == Decimal("109.00")
    assert summary.express_company == "顺丰速运"
    assert summary.logistic_no == "SF100200300400"
    assert summary.latest_logistics_event == {
        "time": "2026-07-21 09:10",
        "text": "快件正在发往上海转运中心",
    }
    serialized_detail = card.model_dump(mode="json")
    assert serialized_detail["total_amount"] == "109.00"
    assert serialized_detail["freight_amount"] == "10.00"
    assert serialized_detail["logistics"]["trace"][-1]["text"] == (
        "快件正在发往上海转运中心"
    )
    assert "receiver_name" not in serialized_detail
    assert "receiver_phone" not in serialized_detail
    assert "receiver_address" not in serialized_detail
    assert "pay_type" not in serialized_detail
    assert "pay_type_label" not in serialized_detail


def test_purchase_history_analysis_focuses_matching_order_ids() -> None:
    orders = [
        OrderCard(
            id=101,
            status=3,
            status_label="已发货",
            pay_amount=Decimal("117.00"),
            created_at=datetime(2026, 7, 23, 12, 0),
            items=[
                OrderItemCard(
                    id=1,
                    sku_id=11,
                    sku_name="Razer 锐影 W02 高清摄像头",
                    sku_specs={},
                    price=Decimal("117.00"),
                    quantity=1,
                )
            ],
            logistics=None,
        ),
        OrderCard(
            id=103,
            status=3,
            status_label="已发货",
            pay_amount=Decimal("149.00"),
            created_at=datetime(2026, 7, 21, 12, 0),
            items=[
                OrderItemCard(
                    id=3,
                    sku_id=13,
                    sku_name="Wooting 山岚 K03 机械键盘 粉色增强版",
                    sku_specs={"color": "粉色"},
                    price=Decimal("149.00"),
                    quantity=1,
                )
            ],
            logistics=None,
        ),
    ]
    plan = order_tools.OrderQueryPlan(
        query="我买过什么键盘吗",
        selection="all",
        operation="match_orders",
        requested_fields=["purchase_history_match"],
    )

    focused = order_tools._analysis_focus_order_ids(plan, orders)

    assert focused == [101, 103]  # Repository filters determine membership; no second heuristic.


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_order_planner_failure_is_not_reported_as_unsupported() -> None:
    class BrokenOrderPlanner:
        async def plan(self, request: OrderLookupInput) -> order_tools.OrderQueryPlan:
            raise RuntimeError("planner unavailable")

    service = order_tools.OrderToolService(
        None,  # type: ignore[arg-type]
        planner=BrokenOrderPlanner(),
    )

    result = await service.lookup(
        OrderLookupInput(user_id=1, query="帮我找找那次比较特别的购买记录")
    )

    assert result.result_type == "planning_failed"
    assert result.diagnostics[0].code == "invalid_order_plan"
    assert all(item.code != "unsupported_query" for item in result.diagnostics)


@pytest.mark.asyncio
async def test_order_lookup_returns_exact_total_for_bounded_recent_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_user_ids: list[int] = []
    cards = [
        OrderCard(
            id=202607020100 + index,
            status=3,
            status_label="已发货",
            total_amount=Decimal("109.00"),
            pay_amount=Decimal("99.00"),
            freight_amount=Decimal("10.00"),
            created_at=datetime(2026, 7, 20 - index, 12, 0),
            items=[],
            logistics=None,
        )
        for index in range(5)
    ]

    class FakeOrderRepository:
        def __init__(self, session: AsyncSession):
            self.session = session

        async def count_orders(self, user_id: int) -> int:
            requested_user_ids.append(user_id)
            return len(cards)

        async def list_recent_orders_page(
            self,
            user_id: int,
            *,
            limit: int,
            offset: int,
        ) -> OrderSearchPage:
            requested_user_ids.append(user_id)
            return OrderSearchPage(
                orders=cards[offset : offset + limit],
                total_count=len(cards),
                offset=offset,
            )

    monkeypatch.setattr(order_tools, "OrderRepository", FakeOrderRepository)
    class Planner:
        async def plan(self, request):
            operation = "total_count" if request.query == "我一共有几个订单" else (
                "detail" if request.query == "所有订单明细" else "list"
            )
            return order_tools.OrderQueryPlan(
                selection="remaining" if request.offset else "recent", operation=operation,
                requested_fields=["order_detail" if operation == "detail"
                                  else "order_count" if operation == "total_count"
                                  else "order_summary"],
            )

    service = order_tools.OrderToolService(None, planner=Planner())

    recent = await service.lookup(
        OrderLookupInput(user_id=1, query="最近两笔订单先给我看看", limit=2)
    )
    count = await service.lookup(OrderLookupInput(user_id=1, query="我一共有几个订单"))
    remaining = await service.lookup(
        OrderLookupInput(user_id=1, query="行，剩下的也都给我吧", offset=2)
    )
    exhausted = await service.lookup(
        OrderLookupInput(user_id=1, query="还有别的吗？继续看", offset=5)
    )
    analysis = await service.lookup(
        OrderLookupInput(user_id=17, query="所有订单明细")
    )

    assert recent.result_type == "order_candidates"
    assert recent.total_match_count == 5
    assert recent.returned_count == 2
    assert recent.is_exhaustive is False
    assert recent.next_offset == 2
    assert [item.id for item in recent.candidates] == [card.id for card in cards[:2]]
    assert count.result_type == "order_count"
    assert count.total_match_count == 5
    assert count.returned_count == 0
    assert remaining.result_type == "order_candidates"
    assert remaining.query_mode == "remaining"
    assert remaining.returned_count == 3
    assert [item.id for item in remaining.candidates] == [card.id for card in cards[2:]]
    assert exhausted.result_type == "order_candidates"
    assert exhausted.query_mode == "remaining"
    assert exhausted.total_match_count == 5
    assert exhausted.returned_count == 0
    assert exhausted.is_exhaustive is True
    assert analysis.result_type == "order_analysis"
    assert analysis.query_mode == "analysis"
    assert analysis.total_match_count == 5
    assert analysis.returned_count == 5
    assert analysis.is_exhaustive is True
    assert len(analysis.analysis_orders) == 5
    assert analysis.candidates == []
    assert requested_user_ids[-1] == 17
    serialized_analysis = analysis.model_dump(mode="json")
    assert "receiver_name" not in str(serialized_analysis)
    assert "receiver_phone" not in str(serialized_analysis)
    assert "receiver_address" not in str(serialized_analysis)


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"query": "列出最近的订单101和订单102"},
    {"query": "查询订单102物流", "order_id": 101},
])
async def test_order_lookup_rejects_conflicting_explicit_selection(monkeypatch, arguments):
    calls = []

    class Repository:
        def __init__(self, session):
            pass

        async def list_recent_orders_page(self, user_id, *, limit, offset):
            calls.append((user_id, limit, offset))
            return OrderSearchPage(orders=[], total_count=0, offset=0)

        async def get_order(self, user_id, order_id):
            calls.append((user_id, order_id))
            return None

    monkeypatch.setattr(order_tools, "OrderRepository", Repository)
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    result = await execute_bound_tool(
        BoundTool(contract, order_tools.OrderToolService(None).lookup), arguments, {"user_id": 7},
    )
    assert calls == []
    assert not result.ok and result.error.code == "invalid_input"
    with pytest.raises(ValidationError):
        OrderLookupInput(user_id=7, **arguments)


@pytest.mark.asyncio
async def test_fast_unsupported_address_request_does_not_query_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RepositoryMustNotBeCreated:
        def __init__(self, session: AsyncSession):
            raise AssertionError("unsupported query must not reach the repository")

    monkeypatch.setattr(order_tools, "OrderRepository", RepositoryMustNotBeCreated)
    service = order_tools.OrderToolService(None, planner=order_tools.LLMOrderQueryPlanner(
        FakeChatModel(json.dumps(order_plan({
            "selection": "all", "operation": "list", "requested_fields": ["receiver_address"],
        }), ensure_ascii=False))
    ))

    result = await service.lookup(
        OrderLookupInput(user_id=1, query="把我每一单最后寄到哪儿整理成地址表")
    )

    assert result.result_type == "unsupported"
    assert result.diagnostics[0].code == "unsupported_query"
    assert "receiver_address" in result.query_plan["unsupported_fields"]


@pytest.mark.asyncio
async def test_estimated_arrival_request_is_unsupported_without_database_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RepositoryMustNotBeCreated:
        def __init__(self, session: AsyncSession):
            raise AssertionError("ETA request must not reach the repository")

    monkeypatch.setattr(order_tools, "OrderRepository", RepositoryMustNotBeCreated)
    service = order_tools.OrderToolService(None, planner=order_tools.LLMOrderQueryPlanner(
        FakeChatModel(json.dumps(order_plan({
            "selection": "explicit", "operation": "detail",
            "requested_fields": ["estimated_delivery_time"],
        }), ensure_ascii=False))
    ))

    result = await service.lookup(
        OrderLookupInput(
            user_id=1,
            order_id=991000000103,
            query="这个多久能到？",
        )
    )

    assert result.result_type == "unsupported"
    assert result.diagnostics[0].code == "unsupported_query"
    assert result.query_plan["supported_fields"] == []
    assert result.query_plan["unsupported_fields"] == ["estimated_delivery_time"]
