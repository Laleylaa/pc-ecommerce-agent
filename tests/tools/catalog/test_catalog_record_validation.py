"""Only explicit same-field source conflicts affect loaded catalog facts."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.repositories.catalog_search import CatalogSearchRepository
from app.schemas.catalog_search import SearchScope


@pytest.mark.contract
@pytest.mark.parametrize("attribute,conflicts", [("否", []), ("是", ["wireless"])])
def test_title_does_not_create_specification_conflicts(attribute, conflicts):
    sku = SimpleNamespace(
        id=389, spu_id=33, title="黑色无线版", price=Decimal(219), stock=1,
        sales_count=0, image_url=None, status=1,
        specs_json={"wireless": "否", "connection_type": "蓝牙"},
    )
    spu = SimpleNamespace(id=33, title="机械键盘", sales_count=0, status=1, detail_html="")
    results = iter([
        [(sku, spu, SimpleNamespace(name="Keychron"), SimpleNamespace(name="键盘"))],
        [(389, "wireless", attribute), (389, "connection_type", "蓝牙")],
    ])

    class Session:
        async def execute(self, statement):
            rows = next(results)
            return SimpleNamespace(all=lambda: rows)

    records = asyncio.run(CatalogSearchRepository(Session()).load(None, SearchScope()))
    assert len(records) == 1
    assert records[0].product.specs == {"wireless": "否", "connection_type": "蓝牙"}
    assert [issue.field for issue in records[0].issues] == conflicts
