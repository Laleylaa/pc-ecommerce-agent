"""Evaluate tricky Order Tool classifications with the real planner and synthetic orders.

Only the synthetic user utterances are sent to the configured planner. The five order records
live in this process and are never sent to the model.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import get_settings
from app.repositories.orders import OrderSearchPage
from app.schemas.order import LogisticsCard, OrderCard, OrderItemCard
from app.tools import orders as order_tools
from app.tools.contracts import build_order_planner
from app.tools.schemas import OrderLookupInput


@dataclass(frozen=True)
class TrickyCase:
    name: str
    query: str
    expected_result_type: str
    expected_diagnostic: str | None = None
    order_id: int | None = None
    expected_unsupported_field: str | None = None
    expected_focus_ids: tuple[int, ...] | None = None


CASES = (
    TrickyCase(
        name="indirect_eta",
        query="照眼下这个走法，我周末前能收着不？",
        order_id=991000000103,
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
        expected_unsupported_field="estimated_delivery_time",
    ),
    TrickyCase(
        name="latest_event_not_eta",
        query="这包裹眼下挪到哪一站了？",
        order_id=991000000103,
        expected_result_type="single_order",
    ),
    TrickyCase(
        name="dispatch_time_with_eta_negation",
        query="不是问哪天送到，我只想知道商家几点把货交给快递",
        order_id=991000000103,
        expected_result_type="single_order",
    ),
    TrickyCase(
        name="indirect_payment_method",
        query="这单钱是从哪种渠道扣的？",
        order_id=991000000103,
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
    ),
    TrickyCase(
        name="mixed_latest_event_and_eta",
        query="先说包裹到哪个节点了，再估一下周五前能不能到",
        order_id=991000000103,
        expected_result_type="single_order",
        expected_diagnostic="partial_unsupported_query",
        expected_unsupported_field="estimated_delivery_time",
    ),
    TrickyCase(
        name="all_item_details_without_standard_keywords",
        query="把五张单里每件货的版本、买几件、当时单价都摊开",
        expected_result_type="order_analysis",
    ),
    TrickyCase(
        name="fuzzy_purchase_history_location",
        query="以前那把 Wooting 粉色键盘压在哪张小票里？",
        expected_result_type="order_analysis",
        expected_focus_ids=(991000000103,),
    ),
    TrickyCase(
        name="indirect_buyer_message",
        query="下单那会儿我给店家留的那句话还能翻出来不？",
        expected_result_type="unsupported",
        expected_diagnostic="unsupported_query",
    ),
    TrickyCase(
        name="five_order_summary_not_detail",
        query="五笔订单先只给我编号、状态和金额，别展开商品",
        expected_result_type="order_candidates",
    ),
    TrickyCase(
        name="mixed_first_item_and_address",
        query="每单买的第一件东西和寄去哪儿并排列给我",
        expected_result_type="order_candidates",
        expected_diagnostic="partial_unsupported_query",
    ),
)


def _synthetic_orders() -> list[OrderCard]:
    anchor = datetime(2026, 7, 23, 7, 49)
    names = (
        "Razer 锐影 W02 高清摄像头 银色电竞版",
        "Logitech G502 Hero 鼠标 黑色版",
        "Wooting 山岚 K03 机械键盘 粉色增强版",
        "SteelSeries Arctis Nova 无线耳机",
        "AOC 曜屏 D07 显示器 白色套装版",
    )
    cards: list[OrderCard] = []
    for index, name in enumerate(names, start=1):
        created_at = anchor - timedelta(days=index - 1)
        shipped = index in {3, 4}
        cards.append(
            OrderCard(
                id=991000000100 + index,
                status=3 if shipped else 2,
                status_label="已发货" if shipped else "待发货",
                total_amount=Decimal(100 + index * 10),
                pay_amount=Decimal(100 + index * 10),
                freight_amount=Decimal("10.00"),
                created_at=created_at,
                pay_at=created_at + timedelta(minutes=8),
                delivery_at=(created_at + timedelta(hours=6) if shipped else None),
                items=[
                    OrderItemCard(
                        id=994000000100 + index,
                        sku_id=9000 + index,
                        sku_name=name,
                        sku_specs={"synthetic": "true", "variant": f"v{index}"},
                        price=Decimal(90 + index * 10),
                        quantity=1,
                    )
                ],
                logistics=(
                    LogisticsCard(
                        express_company="合成测试快递",
                        logistic_no=f"SYN-{index:02d}",
                        status=2,
                        trace=[
                            {
                                "time": created_at.isoformat(),
                                "text": "合成订单已创建",
                            },
                            {
                                "time": (created_at + timedelta(hours=6)).isoformat(),
                                "text": "合成订单运输中",
                            },
                        ],
                    )
                    if shipped
                    else None
                ),
            )
        )
    return cards


class SyntheticOrderRepository:
    orders = _synthetic_orders()

    def __init__(self, session: Any):
        self.session = session

    async def count_orders(self, user_id: int) -> int:
        return len(self.orders)

    async def get_order(self, user_id: int, order_id: int) -> OrderCard | None:
        return next((order for order in self.orders if order.id == order_id), None)

    async def latest_order(self, user_id: int) -> OrderCard | None:
        return self.orders[0]

    async def list_recent_orders_page(
        self,
        user_id: int,
        *,
        limit: int,
        offset: int,
    ) -> OrderSearchPage:
        return OrderSearchPage(
            orders=self.orders[offset : offset + limit],
            total_count=len(self.orders),
            offset=offset,
        )


def _case_result(
    case: TrickyCase,
    output: Any,
) -> dict[str, Any]:
    diagnostics = [item.code for item in output.diagnostics]
    unsupported_fields = list(output.query_plan.get("unsupported_fields") or [])
    actual_focus_ids = list(output.focus_order_ids)
    failures: list[str] = []
    if output.result_type != case.expected_result_type:
        failures.append(
            f"result_type expected {case.expected_result_type}, got {output.result_type}"
        )
    if case.expected_diagnostic is None:
        if diagnostics:
            failures.append(f"expected no diagnostics, got {diagnostics}")
    elif case.expected_diagnostic not in diagnostics:
        failures.append(
            f"expected diagnostic {case.expected_diagnostic}, got {diagnostics}"
        )
    if (
        case.expected_unsupported_field is not None
        and case.expected_unsupported_field not in unsupported_fields
    ):
        failures.append(
            f"expected unsupported field {case.expected_unsupported_field}, "
            f"got {unsupported_fields}"
        )
    if (
        case.expected_focus_ids is not None
        and actual_focus_ids != list(case.expected_focus_ids)
    ):
        failures.append(
            f"focus_order_ids expected {list(case.expected_focus_ids)}, "
            f"got {actual_focus_ids}"
        )
    return {
        "case": case.name,
        "query": case.query,
        "status": "PASS" if not failures else "FAIL",
        "planner": output.query_plan.get("planner"),
        "selection": output.query_plan.get("selection"),
        "operation": output.query_plan.get("operation"),
        "requested_fields": output.query_plan.get("requested_fields"),
        "supported_fields": output.query_plan.get("supported_fields"),
        "unsupported_fields": unsupported_fields,
        "result_type": output.result_type,
        "query_mode": output.query_mode,
        "diagnostics": diagnostics,
        "focus_order_ids": actual_focus_ids,
        "failures": failures,
    }


async def main() -> int:
    settings = get_settings()
    planner = build_order_planner(settings)
    if planner is None:
        raise RuntimeError("A configured LLM order planner is required")

    original_repository = order_tools.OrderRepository
    order_tools.OrderRepository = SyntheticOrderRepository
    results: list[dict[str, Any]] = []
    try:
        service = order_tools.OrderToolService(None, planner=planner)  # type: ignore[arg-type]
        for case in CASES:
            output = await service.lookup(
                OrderLookupInput(
                    user_id=1,
                    order_id=case.order_id,
                    query=case.query,
                )
            )
            results.append(_case_result(case, output))
    finally:
        order_tools.OrderRepository = original_repository

    print(json.dumps(results, ensure_ascii=False, indent=2))
    failures = [result for result in results if result["status"] == "FAIL"]
    print(
        json.dumps(
            {
                "total": len(results),
                "passed": len(results) - len(failures),
                "failed": len(failures),
                "failed_cases": [result["case"] for result in failures],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
