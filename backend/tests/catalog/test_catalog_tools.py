import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy.dialects import postgresql

import app.tools.catalog as catalog_tools
from app.core.config import Settings
from app.repositories.catalog import CatalogRepository
from app.schemas.catalog import ProductCard, ProductSearchRequest
from app.schemas.catalog_search import CatalogSearchRequest, CatalogSearchResult
from app.tools.catalog import (
    CatalogToolService,
    LLMCatalogQueryPlanner,
    ProductQueryPlan,
    validate_catalog_sql,
    validate_product_query_plan,
)
from app.tools.contracts import (
    BoundTool,
    DefaultToolContractProvider,
    build_catalog_planner,
    execute_bound_tool,
)
from app.tools.schemas import (
    CatalogDependencyArtifact,
    CatalogFacetInput,
    CatalogToolRuntimeContext,
)
from tests.support.tool_plan_fixtures import facet_plan
from tests.support.tool_planning import scripted_review

TOOL_TEST_SETTINGS = Settings(llm_api_key="", catalog_llm_planner_enabled=False)


def test_catalog_exclusions_filter_brand_and_usage_matches() -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Logitech Office Mouse",
            brand="Logitech",
            category="mouse",
            price="199.00",
            stock=5,
            sales_count=10,
            specs={"usage": "office"},
        ),
        ProductCard(
            spu_id=2,
            sku_id=22,
            title="Razer Gaming Mouse",
            brand="Razer",
            category="mouse",
            price="299.00",
            stock=5,
            sales_count=20,
            specs={"usage": "gaming"},
        ),
    ]

    assert [
        item.sku_id
        for item in catalog_tools._filter_excluded_preferences(products, ["Logitech"], [])
    ] == [22]
    assert [
        item.sku_id for item in catalog_tools._filter_excluded_preferences(products, [], ["gaming"])
    ] == [11]


def test_spu_aggregation_preserves_real_variant_combinations() -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Series A Wired Black",
            brand="Brand A",
            category="mouse",
            price="100.00",
            stock=8,
            sales_count=500,
            specs={
                "connection_type": "Wired",
                "color": "Black",
                "max_dpi": "26000",
            },
        ),
        ProductCard(
            spu_id=1,
            sku_id=12,
            title="Series A Bluetooth Blue",
            brand="Brand A",
            category="mouse",
            price="120.00",
            stock=0,
            sales_count=500,
            specs={
                "connection_type": "Bluetooth",
                "color": "Blue",
                "max_dpi": "26000",
            },
        ),
        ProductCard(
            spu_id=2,
            sku_id=21,
            title="Series B Bluetooth White",
            brand="Brand B",
            category="mouse",
            price="130.00",
            stock=3,
            sales_count=400,
            specs={
                "connection_type": "Bluetooth",
                "color": "White",
                "max_dpi": "35000",
            },
        ),
    ]

    series = catalog_tools._aggregate_product_series(
        products,
        [1, 2],
        {1: "Series A", 2: "Series B"},
    )

    assert series[0].common_specs == {"max_dpi": "26000"}
    assert [
        value.value for value in series[0].option_specs["connection_type"].values
    ] == ["Bluetooth", "Wired"]
    assert {
        (variant.specs["connection_type"], variant.specs["color"])
        for variant in series[0].variants
    } == {("Wired", "Black"), ("Bluetooth", "Blue")}


@pytest.mark.asyncio
async def test_sku_recommendation_ranking_keeps_top_selling_sku_per_spu() -> None:
    products = [
        ProductCard(
            spu_id=1,
            sku_id=11,
            title="Series A top variant",
            brand="Brand A",
            category="mouse",
            price="199.00",
            stock=10,
            sku_sales_count=900,
            sales_count=1000,
            specs={"connection_type": "Wireless"},
        ),
        ProductCard(
            spu_id=1,
            sku_id=12,
            title="Series A second variant",
            brand="Brand A",
            category="mouse",
            price="189.00",
            stock=12,
            sku_sales_count=800,
            sales_count=1000,
            specs={"connection_type": "Wireless"},
        ),
        ProductCard(
            spu_id=2,
            sku_id=21,
            title="Series B top variant",
            brand="Brand B",
            category="mouse",
            price="209.00",
            stock=8,
            sku_sales_count=700,
            sales_count=900,
            specs={"connection_type": "Wireless"},
        ),
    ]

    class FakeRepository(CatalogRepository):
        async def _eligible_products_for_ranking(self, request):
            return list(products)

    page = await FakeRepository(SimpleNamespace()).search_products_with_total(
        ProductSearchRequest(query="", category="mouse", sort="sales", limit=2),
        distinct_spu=True,
    )

    assert [product.sku_id for product in page.products] == [11, 21]
    assert page.total_count == 2


class FakeChatResponse:
    def __init__(self, content: str):
        self.content = content


class FakeChatModel:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def ainvoke(self, messages):
        if response := scripted_review(messages):
            self.review_calls = [*getattr(self, "review_calls", []), list(messages)]
            return response
        self.calls.append(messages)
        return FakeChatResponse(self.content)


def test_catalog_public_schemas_are_query_first() -> None:
    provider = DefaultToolContractProvider()
    search = provider.get_contract("catalog_search")
    facets = provider.get_contract("catalog_facets")
    assert search is not None
    assert facets is not None

    search_fields = set(search.public_input_model.model_json_schema()["properties"])
    facet_fields = set(facets.public_input_model.model_json_schema()["properties"])

    assert search_fields == {"query", "category_hint", "limit", "scope"}
    assert facet_fields == {"query", "category_hint", "scope", "limit"}
    assert search.internal_input_model is CatalogSearchRequest
    assert search.runtime_fields == ()
    assert facets.runtime_fields == ("catalog_context",)


def test_catalog_context_does_not_mix_memory_into_dependency_artifacts() -> None:
    context = CatalogToolRuntimeContext.model_validate(
        {
            "memory": {
                "query_plan": {"selection_scope": "spu"},
                "candidate_sku_ids": [101],
                "candidate_spu_ids": [10],
                "candidate_display": [
                    {
                        "sku_id": 101,
                        "spu_id": 10,
                        "title": "Current Mouse",
                        "entity_scope": "spu",
                    }
                ],
            },
            "dependency_artifacts": [
                {
                    "task_id": "task_1",
                    "artifact_type": "products",
                    "products": [
                        {
                            "sku_id": 202,
                            "spu_id": 20,
                            "title": "Second-ranked Mouse",
                            "brand": "E2E",
                            "category": "mouse",
                            "price": "299.00",
                            "stock": 10,
                            "sales_count": 800,
                            "sku_sales_count": 80,
                            "specs": {},
                        }
                    ],
                    "selection_scope": "spu",
                }
            ],
        }
    )

    targets, scope = catalog_tools._resolved_catalog_request_context(
        "和销量第二的鼠标对比一下",
        context,
    )

    assert [item.model_dump(mode="json") for item in targets] == [
        {
            "sku_id": 202,
            "spu_id": 20,
            "source": "current_turn_artifact",
        },
    ]
    assert scope == "spu"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["catalog_search"])
async def test_catalog_contract_injects_trusted_context_after_public_validation(
    tool_name: str,
) -> None:
    contract = DefaultToolContractProvider().get_contract(tool_name)
    assert contract is not None
    captured_input: BaseModel | None = None

    async def handler(request):
        nonlocal captured_input
        captured_input = request
        return CatalogSearchResult(
            result_type="empty", status="unsupported",
            reason_code="request_not_supported", recommended_action="explain_limitation",
        )

    catalog_context = CatalogToolRuntimeContext(
        dependency_artifacts=[
            CatalogDependencyArtifact(
                task_id="task_1",
                selected_sku_ids=[101],
                selected_spu_ids=[10],
                selection_scope="sku",
            )
        ]
    )
    result = await execute_bound_tool(
        BoundTool(contract, handler),
        {"query": "测试目录查询", "limit": 3},
        {
            "user_id": 7,
            "catalog_context": catalog_context,
        },
    )

    assert result.ok is True
    assert captured_input is not None
    assert "catalog_context" not in captured_input.model_dump(mode="json")

    forged = await execute_bound_tool(
        BoundTool(contract, handler),
        {
            "query": "测试目录查询",
            "limit": 3,
            "catalog_context": catalog_context.model_dump(mode="json"),
        },
        {"user_id": 7, "catalog_context": CatalogToolRuntimeContext()},
    )
    assert forged.ok is False
    assert forged.error is not None
    assert forged.error.code == "invalid_input"


def test_catalog_search_rejects_non_whitelisted_ranking_metric() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="碳足迹最低的键盘是哪款",
            category="keyboard",
            requested_fields=["product_identity", "price"],
            result_purpose="ranking",
            ranking={
                "scope": "spu",
                "metric": "price",
                "direction": "asc",
                "rank": 1,
                "count": 1,
            },
        )
    )

    assert plan.supported is False
    assert plan.unsupported_reason == "unsupported catalog ranking metric: 碳足迹"


@pytest.mark.asyncio
async def test_catalog_facets_uses_llm_for_dimension_and_scope() -> None:
    chat = FakeChatModel(json.dumps(facet_plan({
        "facet": "spec_value", "category": "键盘", "spec_key": "switches",
    }), ensure_ascii=False))
    service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(chat))  # type: ignore[arg-type]

    plan = await service._safe_plan_facets(
        CatalogFacetInput(query="键盘都能选什么轴体", limit=20)
    )

    assert plan.planner == "llm"
    assert plan.facet == "spec_value"
    assert plan.category == "keyboard"
    assert plan.spec_key == "switches"
    assert plan.count_scope == "spu"


def test_build_catalog_planner_is_opt_in() -> None:
    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=False)  # type: ignore[arg-type]
    )

    assert planner is None


def test_build_catalog_planner_uses_llm_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = FakeChatModel('{"category":"mouse"}')
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings, **kwargs: chat)

    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=True)  # type: ignore[arg-type]
    )

    assert isinstance(planner, LLMCatalogQueryPlanner)
    assert planner.chat_model is chat


def test_build_catalog_planner_skips_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.contracts.build_chat_model", lambda settings, **kwargs: None)

    planner = build_catalog_planner(
        SimpleNamespace(catalog_llm_planner_enabled=True)  # type: ignore[arg-type]
    )

    assert planner is None


@pytest.mark.parametrize(
    ("category", "raw_key", "expected_key"),
    [
        ("mouse", "重量", "weight_g"),
        ("keyboard", "轴体", "switches"),
        ("headset", "麦克风", "microphone"),
        ("monitor", "刷新率", "refresh_rate"),
        ("speaker", "功率", "power_w"),
        ("webcam", "帧率", "frame_rate"),
    ],
)
def test_catalog_filter_aliases_cover_common_category_specs(
    category: str, raw_key: str, expected_key: str
) -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="find products",
            category=category,
            filters={raw_key: "demo"},
        )
    )

    assert plan.filters == {expected_key: "demo"}


def test_catalog_filter_alias_still_respects_category_whitelist() -> None:
    with pytest.raises(ValueError, match="unsupported filters for mouse"):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find mouse",
                category="mouse",
                filters={"刷新率": "144Hz"},
            )
        )


@pytest.mark.parametrize(
    ("category", "raw_key", "raw_value", "expected_filters"),
    [
        ("monitor", "刷新率", "144 赫兹", {"refresh_rate": "144Hz"}),
        ("monitor", "分辨率", "2k", {"resolution": "2560x1440"}),
        ("keyboard", "轴体", "红", {"switches": "红轴"}),
        ("keyboard", "背光", "有", {"backlit": "Yes"}),
        ("mouse", "颜色", "黑", {"color": "黑色"}),
        ("speaker", "power_w", "30W", {"power_w": "30"}),
    ],
)
def test_catalog_filter_values_are_normalized(
    category: str, raw_key: str, raw_value: str, expected_filters: dict[str, str]
) -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="find products",
            category=category,
            filters={raw_key: raw_value},
        )
    )

    assert plan.filters == expected_filters
    assert plan.normalization_debug["filter_aliases"][0]["to"] == {
        "key": next(iter(expected_filters)),
        "value": next(iter(expected_filters.values())),
    }


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("罗技", "Logitech"),
        ("雷蛇", "Razer"),
        ("赛睿", "SteelSeries"),
        ("脉冲星", "Pulsar"),
        ("凯酷", "Keychron"),
        ("艾酷", "Akko"),
        ("极度未知", "HyperX"),
        ("索尼", "Sony"),
        ("冠捷", "AOC"),
        ("华硕", "ASUS"),
        ("戴尔", "Dell"),
        ("乐金", "LG"),
        ("漫步者", "Edifier"),
        ("博士", "Bose"),
        ("创新", "Creative"),
        ("爱乐图", "Elgato"),
        ("圆刚", "AVerMedia"),
    ],
)
def test_catalog_brand_aliases_are_normalized(alias: str, expected: str) -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(query=f"{alias}无线鼠标", category="mouse", brands=[alias])
    )

    assert plan.brands == [expected]


def test_catalog_sql_guard_rejects_unsafe_sql() -> None:
    validate_catalog_sql("SELECT sku.id FROM sku LIMIT 5")

    with pytest.raises(ValueError):
        validate_catalog_sql("SELECT * FROM order_info LIMIT 5")

    with pytest.raises(ValueError):
        validate_catalog_sql("DELETE FROM sku")

    with pytest.raises(ValueError):
        validate_catalog_sql("SELECT sku.id FROM sku")


def test_product_query_guard_rejects_unsupported_fields() -> None:
    with pytest.raises(ValueError):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find products",
                filters={"credit_card": "anything"},
            )
        )

    with pytest.raises(ValueError):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find products",
                category="order",
            )
        )


def test_direct_sku_lookup_statement_requires_active_sku_and_spu() -> None:
    statement = catalog_tools._active_sku_rows_statement([11, 22])
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "sku.status" in sql
    assert "spu.status" in sql
