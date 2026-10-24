"""Structural fixtures only: no repositories, domain services or model calls."""
from app.agent.loop.tool_adapter import AdmissionError, QueryToolAdapter
from app.tools.contracts import (
    BoundTool,
    ToolCatalog,
    ToolRuntimeContext,
    default_tool_contracts,
)
from app.tools.schemas import OrderLookupOutput

TOOL_ARGUMENTS = {
    name: {"query": "synthetic"} for name in (
        "catalog_search", "catalog_lookup", "catalog_facets", "order_lookup",
        "policy_search", "knowledge_search",
    )
}


def admit_goal(goal_id):
    if goal_id not in {"g1", "g2"}:
        raise AdmissionError("goal_not_admitted")


def make_catalog(monkeypatch=None):
    async def order(request):
        return OrderLookupOutput(result_type="not_found")

    async def forbidden(request):
        raise AssertionError("structural fixture must not execute this domain")

    return ToolCatalog([BoundTool(c, order if c.name == "order_lookup" else forbidden)
                        for c in default_tool_contracts()])


def adapter(catalog, **kwargs):
    return QueryToolAdapter(catalog, ToolRuntimeContext(user_id=41),
                            admit_goal=admit_goal, **kwargs)
