"""Root-cause regressions with scripted models and isolated catalog/SQL data."""


import json
from types import SimpleNamespace

from app.repositories.catalog_search import CatalogRecord
from app.schemas.catalog import ProductCard


def scripted_review(messages):
    """Approving reviewer for execution-only fixtures, not a semantic validator.

    Legacy .calls records generation; .review_calls records review attempts.
    Reviewer behavior has separate scripted business tests.
    """
    payload = json.loads(messages[1].content)
    if "candidate_plan" not in payload:
        return None
    return SimpleNamespace(content=json.dumps({"verdict": "approve"}))


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.review_calls = []

    async def ainvoke(self, messages):
        if response := scripted_review(messages):
            self.review_calls.append(list(messages))
            return response
        self.calls.append(list(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(
            content=response if isinstance(response, str) else json.dumps(response)
        )


class Repository:
    def __init__(self, records=()):
        self.records = list(records)
        self.calls = []

    async def load(self, category, scope):
        self.calls.append((category, scope))
        return [
            r
            for r in self.records
            if (not category or r.product.category == category)
            and (not scope.sku_ids or r.product.sku_id in scope.sku_ids)
            and (not scope.spu_ids or r.product.spu_id in scope.spu_ids)
        ]


def product(sku, spu=1, *, category="键盘", price="100", **specs):
    return CatalogRecord(
        product=ProductCard(
            sku_id=sku,
            spu_id=spu,
            title=f"K01 {sku}",
            spu_title="K01",
            brand="Test",
            category=category,
            price=price,
            stock=1,
            specs=specs,
        )
    )


def conditions():
    return [
        {"key": "color", "values": ["白色"], "evidence": "白色"},
        {"key": "connection_type", "values": ["蓝牙"], "evidence": "蓝牙"},
        {"key": "price", "operator": "lte", "values": ["200"], "evidence": "200元以内"},
    ]
