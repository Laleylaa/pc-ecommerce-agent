from app.schemas.catalog_search import CatalogSearchRequest
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService

from tests.support.tool_plan_fixtures import search_plan
from tests.support.tool_planning import Model, Repository


def soft(key, value, evidence):
    return {
        "key": key,
        "values": [value],
        "evidence": value,
        "strength": "preference",
        "preference_evidence": evidence,
    }


async def search(rows, query, plan, **arguments):
    model = Model(search_plan(plan))
    repository = Repository(rows)
    result = await CatalogSearchService(repository, CatalogSearchPlanner(model)).search(
        CatalogSearchRequest(query=query, **arguments)
    )
    return result, model, repository
