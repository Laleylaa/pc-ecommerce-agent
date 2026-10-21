from datetime import datetime

import pytest
from app.repositories.orders import OrderSearchPage
from app.schemas.order import OrderCard, OrderFilters, OrderItemCard
from app.tools.orders import OrderQueryPlan, OrderToolService
from app.tools.schemas import OrderLookupInput


def order(identity, *, month=8, status=4, name="K87 键盘", pay_at=None):
    return OrderCard(
        id=identity, created_at=datetime(2026, month, 15), status=status,
        status_label={2: "待发货", 3: "已发货", 4: "已完成"}[status], pay_amount=199,
        pay_at=pay_at, logistics=None,
        items=[OrderItemCard(id=identity, sku_id=87, sku_name=name,
                             sku_specs={"color": "白色"}, price=199, quantity=1)],
    )


@pytest.fixture
def lookup(monkeypatch):
    calls = []
    # Another user's same product must never leak through the relaxed search.
    rows = [(7, order(101)), (7, order(102, month=9, status=3)),
            (7, order(103, month=9, name="K98 键盘")), (8, order(999, month=9, status=2))]

    class Repository:
        def __init__(self, session):
            pass

        def matching(self, user_id, filters=None):
            filters = filters or OrderFilters()
            matched = []
            for owner, card in rows:
                timestamp = getattr(card, filters.time_field)
                if owner != user_id:
                    continue
                if filters.statuses and card.status not in filters.statuses:
                    continue
                if filters.start_at and (timestamp is None or timestamp < filters.start_at):
                    continue
                if filters.end_at and (timestamp is None or timestamp >= filters.end_at):
                    continue
                if not any(all(term in item.sku_name for term in filters.product_terms)
                           for item in card.items):
                    continue
                matched.append(card)
            return sorted(matched, key=lambda card: (card.created_at, card.id), reverse=True)

        async def list_recent_orders_page(self, user_id, *, limit, offset, filters=None):
            calls.append((user_id, filters))
            result = self.matching(user_id, filters)
            return OrderSearchPage(result[offset:offset + limit], len(result), offset)

        async def count_orders(self, user_id, *, filters=None):
            calls.append((user_id, filters))
            return len(self.matching(user_id, filters))

        async def latest_order(self, user_id, *, filters=None):
            result = self.matching(user_id, filters)
            return result[0] if result else None

        async def get_order(self, user_id, order_id, *, filters=None):
            calls.append((user_id, filters))
            return next((card for card in self.matching(user_id, filters)
                         if card.id == order_id), None)

    monkeypatch.setattr("app.tools.orders.OrderRepository", Repository)

    async def run(filters, *, operation="match_orders", selection="all", limit=5,
                  unresolved=(), order_id=None):
        class Planner:
            async def plan(self, request):
                return OrderQueryPlan(
                    operation=operation, selection=selection, filters=filters,
                    requested_fields=["order_count" if operation == "total_count"
                                      else "purchase_history_match"],
                    unresolved_requirements=list(unresolved),
                )
        return await OrderToolService(None, planner=Planner()).lookup(OrderLookupInput(
            user_id=7, query="查我之前买的K87订单及已有物流", limit=limit, order_id=order_id,
        ))
    return run, calls, rows


def september(**kwargs):
    return OrderFilters(product_terms=["K87"], start_at=datetime(2026, 9, 1),
                        end_at=datetime(2026, 10, 1), **kwargs)
