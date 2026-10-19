import pytest

from app.tools.orders import _extract_order_id


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("查一下订单 202607020001", 202607020001),
        ("我的订单号是202607020001，谢谢", 202607020001),
        ("预算 300，订单 202607020001", 202607020001),
        ("预算300以内", None),
        ("订单 202607020001 和 202607020002 都看看", None),
        ("短号 1234567", None),
    ],
)
def test_order_lookup_query_order_id_eval_cases(query: str, expected: int | None) -> None:
    assert _extract_order_id(query) == expected
