"""Synthetic E30 boundary probes; no real model or configured database access.

Run from backend: python -m scripts.audit_tool_repair_boundaries --output PATH
The artifact preserves observed results and explicit expectations; these probes
are defect reproductions, not assertions that the observed behavior is correct.
"""

import argparse
import asyncio
import json
import socket
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncEngine

from app.repositories.catalog_search import CatalogRecord, CatalogSearchRepository
from app.repositories.orders import OrderRepository
from app.schemas.catalog import ProductCard
from app.schemas.catalog_search import FieldIssue, SearchScope
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.orders import LLMOrderQueryPlanner, OrderToolService

# Explicit wire decisions come from test data, never model/schema defaults.
from tests.support.tool_plan_fixtures import facet_plan, order_plan


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return SimpleNamespace(content=json.dumps(self.responses.pop(0)))


def product(sku: int, spu: int = 1, **specs: str) -> CatalogRecord:
    return CatalogRecord(
        product=ProductCard(
            sku_id=sku,
            spu_id=spu,
            title=f"K01 {sku}",
            spu_title="K01",
            brand="Test",
            category="键盘",
            price="100",
            stock=1,
            specs=specs,
        )
    )


def deny_io(*args, **kwargs):
    raise AssertionError("E30 forbids network and configured database access")


async def facet_case(name, query, wire, records, expectation, *, scope=None):
    model = ScriptedModel(wire, wire)
    reads = []

    async def load(session, category, requested_scope):
        reads.append({"category": category, "scope": requested_scope.model_dump()})
        return [
            r
            for r in records
            if (not category or r.product.category == category)
            and (not requested_scope.sku_ids or r.product.sku_id in requested_scope.sku_ids)
            and (not requested_scope.spu_ids or r.product.spu_id in requested_scope.spu_ids)
        ]

    service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(model))
    arguments = {"query": query}
    if scope:
        arguments["scope"] = scope.model_dump()
    contract = DefaultToolContractProvider().get_contract("catalog_facets")
    with patch.object(CatalogSearchRepository, "load", load):
        result = await execute_bound_tool(
            BoundTool(contract, service.facets), arguments, {"user_id": 7}
        )
    return {
        "case": name,
        "query": query,
        "expectation": expectation,
        "model_calls": len(model.calls),
        "reads": reads,
        "execution": result.model_dump(mode="json"),
    }


async def order_case(name, selection):
    model = ScriptedModel(
        order_plan({"selection": selection}),
        order_plan(
            {
                "unresolved_requirements": ["核对时间条件"],
            }
        ),
    )
    query = "去年有哪些订单"
    service = OrderToolService(None, planner=LLMOrderQueryPlanner(model))
    contract = DefaultToolContractProvider().get_contract("order_lookup")
    reads = []

    async def reject_order_read(*args, **kwargs):
        reads.append("unexpected_order_read")
        raise AssertionError("Planning feedback probe must not query orders")

    with ExitStack() as guards:
        for method in ("count_orders", "get_order", "latest_order", "list_recent_orders_page"):
            guards.enter_context(patch.object(OrderRepository, method, reject_order_read))
        result = await execute_bound_tool(
            BoundTool(contract, service.lookup),
            {"query": query},
            {"user_id": 7},
        )
    return {
        "case": name,
        "query": query,
        "expectation": "Invalid model selection gets RULE_ERROR feedback and one repair; no DB",
        "model_calls": len(model.calls),
        "reads": reads,
        "execution": result.model_dump(mode="json"),
    }


async def probe_cases():
    rows = [
        await order_case("order_explicit_selection_without_identity", "explicit"),
        await order_case("order_remaining_without_offset", "remaining"),
        await facet_case(
            "facet_conflicting_identities_bypass_source_checks",
            "请告诉我SPU 1的键盘配列有哪些",
            facet_plan(
                {
                    "selection": "object",
                    "facet": "spec_value",
                    "spec_key": "style",
                    "category": "键盘",
                    "name": "K01",
                    "target": {"kind": "sku", "id": 9999},
                }
            ),
            [product(9999, spu=2, style="75%")],
            "No read of fabricated SKU 9999; multiple identity and source errors cannot be ignored",
        ),
        await facet_case(
            "facet_bare_number_defaults_to_sku",
            "编号1有什么规格",
            facet_plan(
                {"selection": "object", "facet": "spec_key", "target": {"kind": "sku", "id": 1}}
            ),
            [product(1, style="75%")],
            "Bare catalog number defaults to SKU under the user's confirmed business rule",
        ),
        await facet_case(
            "facet_empty_object_specs_report_exhaustive",
            "这款有什么规格",
            facet_plan(),
            [product(1)],
            "Report missing specification data instead of a conclusive no-match",
            scope=SearchScope(sku_ids=[1]),
        ),
        await facet_case(
            "facet_soft_preference_becomes_hard_filter",
            "键盘有哪些布局，最好白色，但其他颜色也可以",
            facet_plan(
                {
                    "facet": "spec_value",
                    "category": "键盘",
                    "spec_key": "style",
                    "conditions": [
                        {
                            "key": "color",
                            "values": ["白色"],
                            "evidence": "最好白色",
                            "strength": "preference",
                            "preference_evidence": "其他颜色也可以",
                        }
                    ],
                }
            ),
            [product(1, color="白色", style="75%"), product(2, color="黑色", style="全尺寸")],
            "Define and honor preference semantics, or explicitly reject unsupported preference; "
            "do not silently execute it as a requirement",
        ),
    ]
    excluded = product(2, color="黑色", style="全尺寸")
    excluded.issues = [
        FieldIssue(sku_id=2, field="style", status="conflict", values=["全尺寸", "75%"])
    ]
    rows.append(
        await facet_case(
            "facet_nonmatching_sku_contaminates_completeness",
            "白色键盘有哪些布局",
            facet_plan(
                {
                    "facet": "spec_value",
                    "category": "键盘",
                    "spec_key": "style",
                    "conditions": [{"key": "color", "values": ["白色"], "evidence": "白色"}],
                }
            ),
            [product(1, color="白色", style="75%"), excluded],
            "A known color mismatch cannot make the white-keyboard facet incomplete "
            "via other conflicts",
        )
    )
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with (
        patch.object(socket.socket, "connect", deny_io),
        patch.object(socket.socket, "connect_ex", deny_io),
        patch.object(AsyncEngine, "connect", deny_io),
    ):
        rows = asyncio.run(probe_cases())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "scope": "E30 synthetic boundary audit; scripted model, in-memory records, "
                "no external I/O",
                "cases": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    for row in rows:
        execution = row["execution"]
        output = execution.get("output") or {}
        print(
            json.dumps(
                {
                    "case": row["case"],
                    "model_calls": row["model_calls"],
                    "reads": row["reads"],
                    "ok": execution["ok"],
                    "result_type": output.get("result_type"),
                    "diagnostics": [d["code"] for d in output.get("diagnostics", [])],
                    "items": [i["value"] for i in output.get("items", [])],
                    "is_exhaustive": output.get("is_exhaustive"),
                    "matching_complete": output.get("matching_complete"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
