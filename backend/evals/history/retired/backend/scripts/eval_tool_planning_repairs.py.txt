"""Read-only model planning evaluation; synthetic requests, no database access.

Run with ``uv run python -m scripts.eval_tool_planning_repairs --output PATH``.
This evaluates condition extraction, not post-tool answers or an entire chat.
"""

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.llm import build_chat_model
from app.schemas.catalog_search import CatalogSearchRequest
from app.tools.catalog import LLMCatalogQueryPlanner
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.orders import LLMOrderQueryPlanner
from app.tools.schemas import CatalogFacetInput, OrderLookupInput


class RecordedModel:
    def __init__(self, model):
        self.model = model
        self.outputs = []
        self.repairs = []

    async def ainvoke(self, messages):
        if len(messages) > 2:
            self.repairs.append(json.loads(messages[-1].content))
        response = await self.model.ainvoke(messages)
        self.outputs.append(response.content)
        return response


CASES = (
    ("search", "找白色蓝牙键盘，200元以内", "键盘", "conditions"),
    ("search", "白色键盘，300元以上，不要蓝牙", "键盘", "negation"),
    ("search", "帮我找个75%布局的蓝牙键盘，预算不超过400元", "鼠标", "layout"),
    ("facets", "白色蓝牙键盘200元以内有哪些布局可选", "键盘", "conditions"),
    ("facets", "蓝牙键盘有哪些75%布局选项，价格不能超过400元", "鼠标", "layout"),
    ("facets", "这款有什么规格", "键盘", "object"),
    ("orders", "去年我一共有几笔订单", None, "year_count"),
    ("orders", "列出2025年3月到5月付款、已完成、商品包括白色键盘的订单", None, "paid_filter"),
)


def has_condition(plan, key, operator, wanted) -> bool:
    for condition in plan.conditions:
        if condition.key != key or condition.operator != operator:
            continue
        for value in condition.values:
            if value == wanted:
                return True
            if key == "price" and re.fullmatch(rf"{wanted}(?:\.0+)?(?:元|CNY)?", value):
                return True
    return False


def assess(plan, expected: str) -> list[str]:
    errors = []
    required = {
        "conditions": [
            ("color", "eq", "白色"),
            ("connection_type", "eq", "蓝牙"),
            ("price", "lte", "200"),
        ],
        "negation": [
            ("color", "eq", "白色"),
            ("connection_type", "ne", "蓝牙"),
            ("price", "gte", "300"),
        ],
        "layout": [
            ("style", "eq", "75%"),
            ("connection_type", "eq", "蓝牙"),
            ("price", "lte", "400"),
        ],
    }.get(expected, [])
    for key, operator, value in required:
        if not has_condition(plan, key, operator, value):
            errors.append(f"missing/wrong condition: {key} {operator} {value}")
    if expected == "object" and (plan.selection != "object" or plan.facet != "spec_key"):
        errors.append("object specs must use object selection and spec_key")
    if expected == "year_count":
        year = datetime.now(ZoneInfo("Asia/Shanghai")).year
        if (
            plan.operation != "total_count"
            or plan.filters.time_field != "created_at"
            or plan.filters.start_at != datetime(year - 1, 1, 1)
            or plan.filters.end_at != datetime(year, 1, 1)
        ):
            errors.append("count or preceding calendar-year interval lost")
    if expected == "paid_filter":
        f = plan.filters
        if (
            f.time_field != "pay_at"
            or f.start_at != datetime(2025, 3, 1)
            or f.end_at != datetime(2025, 6, 1)
            or f.statuses != [4]
        ):
            errors.append("payment interval/status lost")
        if not f.product_terms:
            errors.append("product requirement lost")
    if hasattr(plan, "supported") and not plan.supported:
        errors.append("supported fixture was rejected")
    if hasattr(plan, "disposition") and plan.disposition != "ready":
        errors.append("supported fixture was not ready")
    return errors


async def run(output: Path) -> None:
    settings = get_settings()
    model = build_chat_model(settings, temperature=0, max_retries=0)
    if model is None:
        raise RuntimeError("Model is not configured")
    results = []
    # Sequential calls avoid adding provider load or a new concurrency policy.
    for tool, query, hint, expected in CASES:
        recorded = RecordedModel(model)
        start = asyncio.get_running_loop().time()
        row = {"tool": tool, "query": query, "category_hint": hint, "expected": expected}
        try:
            if tool == "search":
                plan = await CatalogSearchPlanner(recorded).plan(
                    CatalogSearchRequest(
                        query=query,
                        category_hint=hint,
                    )
                )
            elif tool == "facets":
                kwargs = {"scope": {"spu_ids": [1]}} if expected == "object" else {}
                plan = await LLMCatalogQueryPlanner(recorded).plan_facets(
                    CatalogFacetInput(
                        query=query,
                        category_hint=hint,
                        **kwargs,
                    )
                )
            else:
                plan = await LLMOrderQueryPlanner(recorded).plan(
                    OrderLookupInput(user_id=7, query=query)
                )
            row.update(plan=plan.model_dump(mode="json"), failures=assess(plan, expected))
        except Exception as exc:
            row.update(failures=[type(exc).__name__], error_code=getattr(exc, "code", None))
        row.update(
            elapsed=round(asyncio.get_running_loop().time() - start, 3),
            raw_outputs=recorded.outputs,
            repairs=recorded.repairs,
        )
        results.append(row)
        print(
            json.dumps(
                {k: row[k] for k in ("tool", "expected", "failures", "elapsed")}, ensure_ascii=False
            ),
            flush=True,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"scope": "synthetic model planning only; no database or answers", "cases": results},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
