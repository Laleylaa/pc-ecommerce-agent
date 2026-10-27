"""Real bounded planners with scripted malformed output, no live model or database."""
import json

import pytest
from app.schemas.catalog_search import CatalogSearchRequest
from app.tools.catalog.facets_planner import CatalogFacetPlanner
from app.tools.catalog.planner import CatalogSearchPlanner
from app.tools.schemas import CatalogFacetInput

from tests.support.tool_plan_fixtures import facet_plan, search_plan
from tests.support.tool_planning import Model


@pytest.mark.asyncio
@pytest.mark.boundary
@pytest.mark.parametrize('tool', ['search', 'lookup', 'facets'])
async def test_malformed_detail_is_repaired_without_losing_scope(tool):
    scope = {'scope_kind': 'candidates', 'category': None, 'identity_text': None,
             'target': None, 'needs_specifications': False}
    if tool == 'facets':
        detail = facet_plan({'facet': 'brand'})
    else:
        detail = search_plan({'operation': tool, 'requested_fields': ['price']})
    model = Model(scope, '{broken JSON', detail)
    if tool == 'facets':
        request = CatalogFacetInput(query='给定范围的品牌', scope={'spu_ids': [10]})
        result = await CatalogFacetPlanner(model).plan_facets(request)
        assert result.supported is True
    else:
        request = CatalogSearchRequest(query='给定范围商品', scope={'spu_ids': [10]})
        result = await CatalogSearchPlanner(model, operation=tool).plan(request)
        assert result.operation == tool
    assert len(model.calls) == 3  # scope, invalid detail, repaired detail
    assert len(model.review_calls) == 1  # invalid JSON must never reach semantic review
    review = json.loads(model.review_calls[0][1].content)
    assert review['original_request']['scope']['spu_ids'] == [10]
