"""Independent SELECT snapshot and deliberately small business oracles.

No production matcher, planner, schema, normalization, or ranking helpers are imported.
Only the explicit basic scenarios in the case files are supported here.
"""

from collections import defaultdict
from decimal import Decimal

from catalog_evidence import now

SQL = """SELECT s.id sku_id, p.id spu_id, s.title sku_title, p.title spu_title,
 b.name brand, c.name category, s.price::text price, s.stock,
 s.status sku_status, p.status spu_status, s.specs_json,
 coalesce((SELECT jsonb_agg(jsonb_build_object('key',k.name,'value',v.value)
 ORDER BY k.name,v.value) FROM goods_attribute_relation a
 JOIN attribute_key k ON k.id=a.attr_key_id JOIN attribute_value v ON v.id=a.attr_value_id
 WHERE a.sku_id=s.id), '[]'::jsonb) attributes
 FROM sku s JOIN spu p ON p.id=s.spu_id JOIN brand b ON b.id=p.brand_id
 JOIN category c ON c.id=p.category_id ORDER BY s.id"""


def spec(row, key):
    values = [
        str(v)
        for k, v in (row["specs_json"] or {}).items()
        if k == key and v is not None
    ]
    values += [str(a["value"]) for a in row["attributes"] if a["key"] == key]
    if not values or len(set(values)) != 1:
        raise ValueError("basic_fixture_requires_complete_consistent_" + key)
    return values[0]


def derive(scenario, rows):
    active = [r for r in rows if r["sku_status"] == r["spu_status"] == 1]
    if not active:
        raise ValueError("no_active_products")
    model = [r for r in active if r["spu_title"] == "Keychron 星轴 K01 机械键盘"]
    if (scenario.startswith("lookup") or scenario == "facets_colors") and (
        not model or len({r["spu_id"] for r in model}) != 1
    ):
        raise ValueError("named_model_missing_or_ambiguous")
    result = {}
    if scenario.startswith("lookup"):
        sku = model[0]
        if sum(r["sku_title"] == sku["sku_title"] for r in rows) != 1:
            raise ValueError("sku_title_not_unique")
        prices = sorted({Decimal(r["price"]) for r in model})
        result.update(
            model_query=model[0]["spu_title"] + "多少钱？",
            sku_query=sku["sku_title"] + "多少钱，还有多少库存？",
            wireless_query=model[0]["spu_title"] + "支持无线吗？",
            versions_query=model[0]["spu_title"] + "有哪些版本？按价格从低到高列2个。",
            spu_id=sku["spu_id"],
            sku_id=sku["sku_id"],
            minimum=str(prices[0]),
            maximum=str(prices[-1]),
            sku_price=sku["price"],
            stock=str(sku["stock"]),
            count=len(model),
            price_levels=len(prices),
        )
        if scenario == "lookup_wireless":
            values = defaultdict(list)
            for r in model:
                values[spec(r, "wireless")].append(r["sku_id"])
            if set(values) != {"是", "否"}:
                raise ValueError("requires_both_wireless_and_wired_versions")
            result["wireless_values"] = dict(values)
        if scenario == "lookup_versions":
            chosen = sorted(model, key=lambda r: (Decimal(r["price"]), r["sku_id"]))[:2]
            if len(chosen) != 2 or len(prices) != len(model):
                raise ValueError("requires_two_versions_and_unique_prices")
            result["version_ids"] = [r["sku_id"] for r in chosen]
    elif scenario.startswith("search"):
        pool = [r for r in active if r["category"] == "键盘"]
        if scenario == "search_scope":
            ids = sorted({r["spu_id"] for r in pool})[:2]
            if len(ids) != 2:
                raise ValueError("requires_two_models")
            result["scope"] = {"spu_ids": ids}
            pool = [r for r in pool if r["spu_id"] in ids]
        else:
            pool = [r for r in pool if Decimal(r["price"]) <= 300]
        if scenario == "search_white":
            pool = [r for r in pool if spec(r, "color") == "白色"]
        if scenario == "search_wireless":
            pool = [r for r in pool if spec(r, "wireless") == "是"]
        grouped = defaultdict(list)
        for r in pool:
            grouped[r["spu_id"]].append(r)
        ranked = sorted(
            grouped, key=lambda i: (min(Decimal(r["price"]) for r in grouped[i]), i)
        )
        limit = 1 if scenario == "search_scope" else 2
        if len(ranked) < limit:
            raise ValueError("requires_enough_exact_models")
        # Avoid arbitrarily prescribing a tie winner for a user who only asked price order.
        prices = [min(Decimal(r["price"]) for r in grouped[i]) for i in ranked]
        if len(prices) > limit and prices[limit - 1] == prices[limit]:
            raise ValueError("selection_boundary_price_tie")
        result.update(
            spu_ids=sorted(ranked[:limit]),
            selected_sku_ids=sorted(
                r["sku_id"] for i in ranked[:limit] for r in grouped[i]
            ),
            selected_prices=[str(v) for v in prices[:limit]],
            total=len(ranked),
            sku_count=len(pool),
            returned=limit,
        )
    elif scenario.startswith("facets"):
        pool = active
        key = "category" if scenario == "facets_categories" else "brand"
        if scenario == "facets_budget_brands":
            pool = [r for r in pool if Decimal(r["price"]) <= 300]
        if scenario == "facets_colors":
            pool, key = model, "color"
            result["scope"] = {"spu_ids": [model[0]["spu_id"]]}
        groups = defaultdict(list)
        for r in pool:
            groups[spec(r, key) if key == "color" else r[key]].append(r)
        if not groups or len(groups) > 50:
            raise ValueError("basic_facet_requires_nonempty_complete_display")
        result["facet_counts"] = {
            value: {
                "sku_count": len(items),
                "spu_count": len({r["spu_id"] for r in items}),
                "count": len({r["spu_id"] for r in items}),
            }
            for value, items in groups.items()
        }
        result["facet_sources"] = {
            value: {
                "sku_ids": sorted(r["sku_id"] for r in items),
                "spu_ids": sorted({r["spu_id"] for r in items}),
            }
            for value, items in groups.items()
        }
        result["total"] = len(groups)
    else:
        raise ValueError("unknown_oracle_scenario")
    return result


async def read_basis(session, case, binding):
    if case["kind"] == "contract":
        return {
            "eligible": True,
            "oracle": {},
            "parameters": {},
            "reason": None,
            "checked_at": now(),
            "source": "input contract; no product facts required",
        }
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        async with session.begin_nested():
            rows = [dict(r) for r in (await session.execute(text(SQL))).mappings()]
        oracle = derive(case["oracle_scenario"], rows)
        # Raw facts are part of the frozen comparison, not just aggregate answers.
        oracle["_raw_rows"] = rows
        return {
            "eligible": True,
            "oracle": oracle,
            "sql": SQL,
            "parameters": {},
            "checked_at": now(),
            "reason": None,
            "source": "independent catalog SELECT",
        }
    except (SQLAlchemyError, ValueError, TypeError, KeyError) as exc:
        return {
            "eligible": False,
            "reason": "basis_unavailable",
            "sql": SQL,
            "checked_at": now(),
            "error_type": type(exc).__name__,
            "detail": str(exc)
            if isinstance(exc, ValueError)
            else "database read failed",
        }
