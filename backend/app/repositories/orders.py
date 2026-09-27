from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import OrderInfo, OrderItem, OrderLogistics
from app.schemas.order import LogisticsCard, OrderCard, OrderItemCard

ORDER_STATUS_LABELS = {
    1: "待付款",
    2: "待发货",
    3: "已发货",
    4: "已完成",
    5: "已关闭",
}


@dataclass(frozen=True)
class OrderSearchPage:
    orders: list[OrderCard]
    total_count: int
    offset: int


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_order(self, user_id: int, order_id: int) -> OrderCard | None:
        stmt = (
            select(OrderInfo)
            .where(OrderInfo.user_id == user_id, OrderInfo.id == order_id)
            .options(selectinload(OrderInfo.items), selectinload(OrderInfo.logistics))
        )
        order = (await self.session.execute(stmt)).scalar_one_or_none()
        if order is None:
            return None
        return _to_order_card(order)

    async def latest_order(self, user_id: int) -> OrderCard | None:
        stmt = (
            select(OrderInfo)
            .where(OrderInfo.user_id == user_id)
            .order_by(OrderInfo.created_at.desc(), OrderInfo.id.desc())
            .options(selectinload(OrderInfo.items), selectinload(OrderInfo.logistics))
            .limit(1)
        )
        order = (await self.session.execute(stmt)).scalar_one_or_none()
        if order is None:
            return None
        return _to_order_card(order)

    async def list_recent_orders(self, user_id: int, limit: int = 5) -> list[OrderCard]:
        page = await self.list_recent_orders_page(user_id, limit=limit)
        return page.orders

    async def count_orders(self, user_id: int) -> int:
        stmt = select(func.count(OrderInfo.id)).where(OrderInfo.user_id == user_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_recent_orders_page(
        self,
        user_id: int,
        *,
        limit: int = 5,
        offset: int = 0,
    ) -> OrderSearchPage:
        total_count = await self.count_orders(user_id)
        stmt = (
            select(OrderInfo)
            .where(OrderInfo.user_id == user_id)
            .order_by(OrderInfo.created_at.desc(), OrderInfo.id.desc())
            .options(selectinload(OrderInfo.items), selectinload(OrderInfo.logistics))
            .offset(offset)
            .limit(limit)
        )
        orders = (await self.session.execute(stmt)).scalars().all()
        return OrderSearchPage(
            orders=[_to_order_card(order) for order in orders],
            total_count=total_count,
            offset=offset,
        )

    async def item_belongs_to_user(self, user_id: int, order_id: int, order_item_id: int) -> bool:
        stmt = (
            select(OrderItem.id)
            .join(OrderInfo, OrderItem.order_id == OrderInfo.id)
            .where(
                OrderInfo.user_id == user_id,
                OrderInfo.id == order_id,
                OrderItem.id == order_item_id,
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


def _to_order_card(order: OrderInfo) -> OrderCard:
    logistics: OrderLogistics | None = order.logistics
    trace = []
    if logistics and isinstance(logistics.trace_json, list):
        trace = logistics.trace_json

    return OrderCard(
        id=order.id,
        status=order.status,
        status_label=ORDER_STATUS_LABELS.get(order.status, "未知状态"),
        total_amount=order.total_amount,
        pay_amount=order.pay_amount,
        freight_amount=order.freight_amount,
        created_at=order.created_at,
        items=[
            OrderItemCard(
                id=item.id,
                sku_id=item.sku_id,
                sku_name=item.sku_name,
                sku_specs=item.sku_specs,
                price=item.price,
                quantity=item.quantity,
            )
            for item in order.items
        ],
        logistics=(
            LogisticsCard(
                express_company=logistics.express_company,
                logistic_no=logistics.logistic_no,
                status=logistics.status,
                trace=trace,
            )
            if logistics
            else None
        ),
        pay_at=order.pay_at,
        delivery_at=order.delivery_at,
    )
