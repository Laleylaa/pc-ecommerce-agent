"""Synthetic rows and IO doubles; matching/aggregation stay in production services."""
from copy import deepcopy
from decimal import Decimal

from app.repositories.catalog_search import CatalogRecord
from app.schemas.catalog import ProductCard
from app.schemas.catalog_search import SearchPlan
from app.tools.catalog.facets import CatalogFacetService
from app.tools.catalog.facets_plan import FacetQueryPlan
from app.tools.catalog.service import CatalogSearchService
from app.tools.contracts import BoundTool, default_tool_contracts, execute_bound_tool


def row(sku, spu=10, *, price=100, stock=5, sales=10, active=True, **specs):
    return CatalogRecord(product=ProductCard(
        sku_id=sku, spu_id=spu, title=f'测试键盘{spu} 版本{sku}',
        spu_title=f'测试键盘{spu}', brand='测试品牌', category='键盘',
        price=Decimal(str(price)), stock=stock, sales_count=sales, specs=specs,
    ), active=active)


class Rows:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.calls = []

    async def load(self, category, scope, *, sku_ids=None):
        self.calls.append((category, scope.model_dump()))
        return deepcopy([
            r for r in self.rows
            if (not category or r.product.category == category)
            and (not scope.sku_ids or r.product.sku_id in scope.sku_ids)
            and (not scope.spu_ids or r.product.spu_id in scope.spu_ids)
            and (sku_ids is None or r.product.sku_id in sku_ids)
        ])


class FixedPlan:
    """Execution tests only: no claim of natural-language interpretation."""
    def __init__(self, plan):
        self.value = plan

    async def plan(self, request):
        return self.value.model_copy(deep=True)

    plan_facets = plan


async def invoke(monkeypatch, tool, rows, plan, **arguments):
    repository = Rows(rows)
    name = 'catalog_' + tool
    if tool == 'facets':
        monkeypatch.setattr('app.tools.catalog.facets.CatalogSearchRepository',
                            lambda session: repository)
        handler = CatalogFacetService(object(), FixedPlan(FacetQueryPlan(**plan))).facets
    else:
        value = SearchPlan(operation=tool, purpose=tool, **plan)
        handler = CatalogSearchService(repository, FixedPlan(value), operation=tool).search
    contract = next(c for c in default_tool_contracts() if c.name == name)
    result = await execute_bound_tool(
        BoundTool(contract, handler), {'query': '合成执行用例', **arguments}, {'user_id': 41},
    )
    assert result.ok, result.error
    assert result.error is None
    return result.output


def condition(key, values, operator='eq', strength='requirement'):
    return {'key': key, 'values': values, 'operator': operator, 'strength': strength}
