"""Independent core-business expectations over raw, read-only catalog rows.

Explicit scenario predicates, not a second natural-language planner. No production
matcher/field registry is imported. Missing, conflicting or unfamiliar fixture
values reject preparation, rather than silently weakening an expected condition.
"""

import re
from collections import defaultdict
from decimal import Decimal


def numeric(value: str, unit: str = "") -> Decimal:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:" + re.escape(unit) + r")?", value)
    if not match:
        raise ValueError("unsupported_fixture_number:" + value)
    return Decimal(match[1])


def field_values(rows: list[dict], key: str, spec) -> dict:
    values = defaultdict(list)
    for row in rows:
        value = str(row[key]) if key in {"stock", "price"} else spec(row, key)
        values[value].append(row["sku_id"])
    return {value: sorted(ids) for value, ids in values.items()}


def combination_material(rows: list[dict], spec, *, color: str, discovery: bool) -> list[dict]:
    """Raw evidence for explicitly labelled alternatives; never prescribe a winner."""
    material = []
    for row in rows:
        facts = {key: spec(row, key) for key in ("color", "switches")}
        satisfied = {
            "color": facts["color"] == color,
            "switches": facts["switches"] in {"红轴", "线性红轴", "静音红轴"},
        }
        if discovery:
            facts["wireless"] = spec(row, "wireless")
            facts["price"] = row["price"]
            satisfied.update(wireless=facts["wireless"] == "是", price=Decimal(row["price"]) <= 500)
        mismatched = sorted(key for key, matches in satisfied.items() if not matches)
        if len(mismatched) == 1:
            material.append({
                "sku_id": row["sku_id"], "spu_id": row["spu_id"], "price": row["price"],
                "facts": facts, "mismatched_keys": mismatched,
            })
    return material


def search(scenario: str, active: list[dict], spec) -> dict:
    category = {
        "mouse": "鼠标", "monitor": "显示器", "headset": "耳机",
        "speaker": "音箱", "webcam": "摄像头",
    }.get(scenario, "键盘")
    budget = {"mouse": 300, "monitor": 2000, "headset": 400, "speaker": 400}.get(
        scenario, 500
    )
    category_rows = [r for r in active if r["category"] == category]
    extra = {}
    if scenario == "combination":
        extra["alternative_material"] = combination_material(
            category_rows, spec, color="白色", discovery=True,
        )
    pool = [r for r in category_rows if Decimal(r["price"]) <= budget]
    predicates = {
        "brand": lambda r: r["brand"] == "Keychron",
        "exclusion": lambda r: r["brand"] != "Keychron" and spec(r, "color") != "黑色",
        "range": lambda r: 200 <= Decimal(r["price"]) <= 400,
        "combination": lambda r: spec(r, "color") == "白色"
        and spec(r, "wireless") == "是" and spec(r, "switches") in {"线性红轴", "静音红轴", "红轴"},
        "sales": lambda r: True,
        "mouse": lambda r: numeric(spec(r, "weight_g"), "g") <= 60
        and numeric(spec(r, "max_dpi"), "dpi") >= 20000,
        "monitor": lambda r: numeric(spec(r, "size_inch"), "英寸") == 27
        and spec(r, "resolution") in {"2560x1440", "2K", "1440p"}
        and numeric(spec(r, "refresh_rate"), "Hz") >= 144,
        "headset": lambda r: spec(r, "type") == "头戴式" and spec(r, "microphone") == "是",
        "speaker": lambda r: numeric(spec(r, "power_w"), "W") >= 40,
        "webcam": lambda r: numeric(spec(r, "frame_rate"), "fps") >= 60
        and spec(r, "microphone") == "是",
        "combination_black": lambda r: spec(r, "color") == "黑色"
        and spec(r, "wireless") == "是" and spec(r, "switches") in {"线性红轴", "静音红轴", "红轴"},
    }
    pool = [r for r in pool if predicates[scenario](r)]
    groups = defaultdict(list)
    for row in pool:
        groups[row["spu_id"]].append(row)
    minimum = {i: min(Decimal(r["price"]) for r in rs) for i, rs in groups.items()}
    scores = minimum if scenario != "sales" else {
        i: rs[0]["spu_sales_count"] for i, rs in groups.items()
    }
    descending = scenario in {"range", "sales"}
    ranked = sorted(groups, key=lambda i: ((-1 if descending else 1) * scores[i], i))
    limit = 3 if scenario == "sales" else 2
    if len(ranked) < limit and scenario not in {"combination", "combination_black"}:
        raise ValueError("requires_enough_exact_models")
    if scenario == "combination" and ranked:
        raise ValueError("requires_zero_exact_models_for_no_match_case")
    if scenario == "combination_black" and not 0 < len(ranked) < limit:
        raise ValueError("requires_positive_fewer_than_requested_models")
    if len(ranked) > limit and scores[ranked[limit - 1]] == scores[ranked[limit]]:
        raise ValueError("selection_boundary_tie")
    chosen = ranked[:limit]
    return {
        "spu_ids": sorted(chosen), "ordered_spu_ids": chosen,
        "selected_sku_ids": sorted(r["sku_id"] for i in chosen for r in groups[i]),
        "selected_prices": [str(minimum[i]) for i in chosen],
        "total": len(groups), "sku_count": len(pool), "returned": len(chosen), **extra,
    }


LOOKUPS = {
    "partial": ("Keychron 星轴 K01 机械键盘", [], "星轴 K01 多少钱？"),
    "stock": ("Keychron 星轴 K01 机械键盘", ["stock"],
              "Keychron 星轴 K01 机械键盘各版本还有多少库存？"),
    "combination": ("Keychron 星轴 K01 机械键盘", ["color", "switches"],
                    "Keychron 星轴 K01 机械键盘有白色红轴的版本吗？这些版本多少钱？"),
    "monitor": ("Dell 锐屏 D01 显示器", ["size_inch", "resolution", "refresh_rate"],
                "Dell 锐屏 D01 显示器多大、分辨率多少、刷新率多少？请区分版本。"),
    "mouse": ("Logitech 影刃 M01 游戏鼠标", ["weight_g", "max_dpi"],
              "Logitech 影刃 M01 游戏鼠标有多重，最高 DPI 是多少？请区分版本。"),
    "headset": ("Logitech 声场 H01 游戏耳机", ["type", "microphone"],
                "Logitech 声场 H01 游戏耳机是头戴还是入耳，有麦克风吗？请区分版本。"),
    "speaker": ("Edifier 声塔 S01 桌面音箱", ["channels", "power_w"],
                "Edifier 声塔 S01 桌面音箱是几声道、功率多大？请区分版本。"),
    "webcam": ("Logitech 清眸 W01 高清摄像头", ["connection_type", "frame_rate", "microphone"],
               "Logitech 清眸 W01 高清摄像头用什么接口、多少帧、带麦克风吗？请区分版本。"),
    "partial_brand": ("Keychron 星轴 K01 机械键盘", [], "Keychron 星轴 K01 多少钱？"),
    "combination_black": ("Keychron 星轴 K01 机械键盘", ["color", "switches"],
                          "Keychron 星轴 K01 机械键盘有黑色红轴的版本吗？这些版本多少钱？"),
}


def lookup(scenario: str, active: list[dict], spec) -> dict:
    title, fields, query = LOOKUPS[scenario]
    if scenario == "partial":
        candidates = {
            r["spu_id"]: {key: r[key] for key in ("spu_id", "brand", "category")}
            | {"title": r["spu_title"]}
            for r in active if "星轴 K01" in r["spu_title"]
        }
        if len(candidates) < 2:
            raise ValueError("requires_ambiguous_partial_name")
        return {"query": query, "identity_candidates": list(candidates.values())}
    rows = [r for r in active if r["spu_title"] == title]
    if not rows or len({r["spu_id"] for r in rows}) != 1:
        raise ValueError("named_model_missing_or_ambiguous")
    spu_id, extra = rows[0]["spu_id"], {}
    if scenario == "partial_brand":
        name = "Keychron 星轴 K01"
        if {r["spu_id"] for r in active if name in r["spu_title"]} != {rows[0]["spu_id"]}:
            raise ValueError("partial_name_ambiguous")
    if scenario in {"combination", "combination_black"}:
        color = "白色" if scenario == "combination" else "黑色"
        if scenario == "combination":
            extra["alternative_material"] = combination_material(
                rows, spec, color=color, discovery=False,
            )
        rows = [r for r in rows if spec(r, "color") == color
                and spec(r, "switches") in {"红轴", "线性红轴", "静音红轴"}]
    if scenario == "combination" and rows:
        raise ValueError("requires_absent_combination")
    if not rows and scenario != "combination":
        raise ValueError("requires_existing_versions")
    prices = sorted({Decimal(r["price"]) for r in rows})
    return {
        "query": query, "spu_ids": [spu_id],
        "sku_ids": sorted(r["sku_id"] for r in rows), "count": len(rows),
        "minimum": str(prices[0]) if prices else None,
        "maximum": str(prices[-1]) if prices else None, "price_levels": len(prices),
        **{key + "_values": field_values(rows, key, spec) for key in fields},
        **extra,
    }


def facets(scenario: str, active: list[dict], spec) -> dict:
    pool, key, result = active, "brand", {}
    if scenario in {"keyboard_brands", "keys", "switches"}:
        pool = [r for r in pool if r["category"] == "键盘"]
    if scenario == "resolutions":
        pool, key = [r for r in pool if r["category"] == "显示器"], "resolution"
    if scenario == "switches":
        pool = [r for r in pool if Decimal(r["price"]) <= 500 and spec(r, "color") == "白色"]
        key = "switches"
    if scenario == "sku_scope":
        model = [r for r in pool if r["spu_title"] == "Keychron 星轴 K01 机械键盘"]
        if len(model) < 2 or len({r["spu_id"] for r in model}) != 1:
            raise ValueError("requires_two_named_versions")
        pool, key = sorted(model, key=lambda r: r["sku_id"])[:2], "color"
        result["scope"] = {"sku_ids": [r["sku_id"] for r in pool]}
    # Separate each dimension's values and their contributing records.
    groups = defaultdict(lambda: defaultdict(list))
    for row in pool:
        keys = ({*(row["specs_json"] or {}), *(a["key"] for a in row["attributes"])}
                if scenario == "keys" else {key})
        for field in keys:
            value = row["brand"] if field == "brand" else spec(row, field)
            groups[field if scenario == "keys" else value][value].append(row)
    if not groups or len(groups) > 50:
        raise ValueError("requires_nonempty_bounded_facets")
    counts, sources, dimensions = {}, {}, {}
    for name, values in groups.items():
        ids = {r["sku_id"] for rs in values.values() for r in rs}
        spus = {r["spu_id"] for rs in values.values() for r in rs}
        counts[name] = {"sku_count": len(ids), "spu_count": len(spus), "count": len(spus)}
        sources[name] = {"sku_ids": sorted(ids), "spu_ids": sorted(spus)}
        dimensions[name] = {
            v: {"sku_ids": sorted(r["sku_id"] for r in rs),
                "spu_ids": sorted({r["spu_id"] for r in rs})}
            for v, rs in values.items()
        }
    order = sorted(counts, key=lambda v: (-counts[v]["count"], v))
    if scenario == "window" and len(order) <= 2:
        raise ValueError("requires_more_than_two_brands")
    chosen = order[:2] if scenario == "window" else order
    result.update(
        facet_counts={v: counts[v] for v in chosen},
        facet_sources={v: sources[v] for v in chosen},
        dimension_values={v: dimensions[v] for v in chosen},
        total=len(order), returned=len(chosen), exhaustive=len(chosen) == len(order),
    )
    return result


def derive_core(scenario: str, rows: list[dict], spec) -> dict:
    _, tool, name = scenario.split("_", 2)
    active = [r for r in rows if r["sku_status"] == r["spu_status"] == 1]
    if not active:
        raise ValueError("no_active_products")
    return {"search": search, "lookup": lookup, "facets": facets}[tool](name, active, spec)
