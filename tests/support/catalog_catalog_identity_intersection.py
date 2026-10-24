from app.tools.catalog.compatibility import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog.planner import CatalogSearchPlanner
from app.tools.catalog.service import CatalogSearchService
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool

from tests.support.tool_plan_fixtures import facet_plan, search_plan
from tests.support.tool_planning import Model, Repository


async def invoke(monkeypatch, tool, query, rows, *plans, scope=None):
    model, repository = Model(*plans), Repository(rows)
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    handler = (
        CatalogSearchService(repository, CatalogSearchPlanner(model)).search
        if tool == "catalog_search"
        else CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets
    )
    contract = DefaultToolContractProvider().get_contract(tool)
    arguments = {"query": query}
    if scope is not None:
        arguments["scope"] = scope
    result = await execute_bound_tool(BoundTool(contract, handler), arguments, {"user_id": 7})
    return result.model_dump(mode="json"), model, repository


def plan_for(tool, *, name="K01", kind="spu", identity=1, **fields):
    common = {"name": name, "target": {"kind": kind, "id": identity}, **fields}
    if tool == "catalog_search":
        return search_plan({"operation": "lookup", "requested_fields": ["price"], **common})
    return facet_plan({"selection": "object", "facet": "spec_key", **common})
