"""Synthetic orders with one UTC anchor and consistent shipment chronology."""

from datetime import datetime, timedelta
from decimal import Decimal

from app.models import AppUser, OrderInfo, OrderItem, OrderLogistics, Sku
from sqlalchemy.ext.asyncio import AsyncSession


def order_times(anchor: datetime, days_ago: int, status: int) -> dict:
    created = anchor - timedelta(days=days_ago)
    return {
        "created_at": created,
        "pay_at": created + timedelta(minutes=5) if status in {2, 3, 4} else None,
        "delivery_at": created + timedelta(hours=6) if status in {3, 4} else None,
    }


async def create_orders(
    session: AsyncSession, users: list[AppUser], skus: list[Sku], anchor: datetime,
) -> int:
    count = 0
    for ordinal, user in enumerate(users):
        statuses = (3,) if ordinal == 0 else (1, 2, 3, 4, 5)
        for sequence, status in enumerate(statuses, start=1):
            sku = skus[(ordinal * 12 + sequence - 1) % len(skus)]
            times = order_times(anchor, sequence, status)
            order = OrderInfo(
                id=202607020001 + count, user_id=user.id,
                total_amount=sku.price,
                pay_amount=sku.price if status in {2, 3, 4} else Decimal("0"),
                freight_amount=Decimal("0"), pay_type=2 if status in {2, 3, 4} else 0,
                status=status, receiver_name=user.display_name,
                receiver_phone="00000000000", receiver_address="演示地址（非真实收货地址）",
                **times,
            )
            session.add(order)
            await session.flush()
            session.add(OrderItem(
                order_id=order.id, spu_id=sku.spu_id, sku_id=sku.id,
                sku_name=sku.title, sku_specs=sku.specs_json, sku_image=sku.image_url,
                price=sku.price, quantity=1,
            ))
            if status in {3, 4}:
                shipped = times["delivery_at"]
                session.add(OrderLogistics(
                    order_id=order.id, express_company="演示物流", express_code="DEMO",
                    logistic_no=f"DEMO{order.id}", status=3 if status == 4 else 2,
                    trace_json=[
                        {"time": shipped.isoformat(), "text": "快件已揽收"},
                        {"time": (shipped + timedelta(hours=2)).isoformat(),
                         "text": "快件已签收" if status == 4 else "快件正在发往转运中心"},
                    ],
                ))
            count += 1
    return count
