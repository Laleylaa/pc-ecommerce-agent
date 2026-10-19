"""Explicit product identity syntax shared by planning and search; no business I/O."""

import re
from typing import Literal

PRODUCT_REFERENCE_PREFIX = (
    r"(SKU|SPU|商品编号|(?<!订单)(?<!物流)(?<!快递)(?<!运单)编号)"
    r"\s*(?:编号\s*)?[:：#]?\s*"
)


def product_reference_kind(label: str) -> Literal["sku", "spu"]:
    """Apply the catalog's declared label policy; never inspect product existence."""
    return {"sku": "sku", "spu": "spu", "商品编号": "sku", "编号": "sku"}[label.casefold()]


PRODUCT_REFERENCE = re.compile(
    rf"(?<![A-Za-z0-9_]){PRODUCT_REFERENCE_PREFIX}(\d+)(?![\dA-Za-z_]|\.\d)",
    re.IGNORECASE,
)


def explicit_product_ids(text: str) -> set[tuple[str, int]]:
    """Extract labelled identities, never ordinals, model numbers, or product names."""
    return {
        (product_reference_kind(label), int(identity))
        for label, identity in PRODUCT_REFERENCE.findall(text)
    }


PRODUCT_IDENTITY_GUIDANCE = """SKU是具体可售版本，SPU是型号；展示序号不是商品编号。
SKU 101、SKU编号101、SKU编号：101等明确写法表示相同身份；SPU同理。
商品查询中的“编号101”“商品编号101”未标类型时按SKU101解释；明确SPU时仍按SPU。
编排可保留该原文交给工具，不需要先把编号改写成SKU；订单/物流编号不适用此规则。
不从型号名称中的数字猜编号。型号卡的代表SKU不代表整个型号；已返回版本不等于全部版本。
历史对象是只读身份依据，不是默认搜索范围；引用对象、参照对象和硬查询范围必须区分。"""

PRODUCT_IDENTITY_INTERSECTION_GUIDANCE = """同一对象的名称和明确编号可以同时提供：
name和target都保留，
由程序按编号读取并核对名称，只使用两者交集；不能因同时出现就判为多个目标或删除其中一个。
例如“云梭M08（SPU 32）的价格”是一个对象；“云梭M08和SPU 31各多少钱”是独立对象，须拆分。
型号名可与该型号的SKU编号共同限定版本；是否属于同一商品由程序查库确认，不由模型猜测。
名称、编号必须来自query；名称错写、编号编造属于计划错误，不是商品不存在或需要用户改条件。
scope仍是调用方给定的硬范围，不能因名称/编号存在就忽略或扩大它。"""
