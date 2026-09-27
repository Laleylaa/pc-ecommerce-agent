from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.catalog import _catalog_search_statement, _matches_single_filter
from app.schemas.catalog import ProductSearchRequest
from app.tools.catalog import (
    ProductQueryPlan,
    RuleBasedCatalogQueryPlanner,
    _facet_query_plan,
    _plan_to_product_search,
    validate_product_query_plan,
)
from app.tools.schemas import CatalogFacetInput, CatalogSearchInput


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


PLANNER_GOLDEN_CASES = [
    (
        "wireless mouse under 300 from Logitech",
        {
            "category": "mouse",
            "brands": ["Logitech"],
            "max_price": Decimal("300"),
            "filters": {"connection_type": "Wireless"},
        },
    ),
    (
        "144Hz 2K monitor",
        {
            "category": "monitor",
            "filters": {"refresh_rate": "144Hz", "resolution": "2560x1440"},
        },
    ),
    (
        "gaming keyboard with red switches",
        {
            "category": "keyboard",
            "filters": {"switches": "Red"},
            "usage_scenario": "gaming",
        },
    ),
    (
        "wireless headset with microphone",
        {
            "category": "headset",
            "filters": {"connection_type": "Wireless", "microphone": "Yes"},
        },
    ),
    (
        "bluetooth speaker",
        {
            "category": "speaker",
            "filters": {"connection_type": "蓝牙"},
        },
    ),
    (
        "4K USB-C 60fps webcam",
        {
            "category": "webcam",
            "filters": {
                "connection_type": "USB-C",
                "resolution": "4K",
                "frame_rate": "60fps",
            },
        },
    ),
    (
        "20W speaker",
        {
            "category": "speaker",
            "filters": {"power_w": "20"},
        },
    ),
    (
        "webcam 1080p 60fps with microphone",
        {
            "category": "webcam",
            "filters": {"resolution": "1080p", "frame_rate": "60fps", "microphone": "Yes"},
        },
    ),
    (
        "black webcam",
        {
            "category": "webcam",
            "filters": {"color": "Black"},
        },
    ),
    (
        "有 300 块以内的罗技无线鼠标吗",
        {
            "category": "mouse",
            "brands": ["Logitech"],
            "filters": {"connection_type": "Wireless"},
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("query", "expected"), PLANNER_GOLDEN_CASES)
async def test_catalog_rule_planner_golden_cases(query: str, expected: dict) -> None:
    raw_plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query=query, limit=3)
    )
    plan = validate_product_query_plan(raw_plan)

    for key, expected_value in expected.items():
        actual = getattr(plan, key)
        if key == "filters":
            for filter_key, filter_value in expected_value.items():
                assert actual[filter_key] == filter_value
        else:
            assert actual == expected_value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_usage"),
    [
        ("推荐办公键盘", "office"),
        ("FPS 电竞显示器", "gaming"),
        ("开会用的摄像头", "video_meeting"),
        ("直播摄像头", "live_streaming"),
        ("办公开会用的摄像头", "video_meeting"),
        ("办公直播摄像头", "live_streaming"),
    ],
)
async def test_rule_planner_normalizes_common_usage_scenarios(
    query: str, expected_usage: str
) -> None:
    raw_plan = await RuleBasedCatalogQueryPlanner().plan_search(
        CatalogSearchInput(query=query, limit=3)
    )

    assert raw_plan.usage_scenario == expected_usage


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "what mouse brands do you sell",
            {"facet": "brand", "category": "mouse", "supported": True},
        ),
        (
            "what peripheral categories does Razer sell",
            {"facet": "category", "brand": "Razer", "supported": True},
        ),
        (
            "what keyboard switches are available",
            {
                "facet": "spec_value",
                "category": "keyboard",
                "spec_key": "switches",
                "supported": True,
            },
        ),
        (
            "what monitor switches are available",
            {
                "facet": "spec_value",
                "category": "monitor",
                "spec_key": "switches",
                "supported": False,
                "unsupported_reason": "unsupported spec_key for monitor",
            },
        ),
    ],
)
def test_catalog_facet_planner_golden_cases(query: str, expected: dict) -> None:
    plan = _facet_query_plan(CatalogFacetInput(query=query))

    for key, expected_value in expected.items():
        actual = getattr(plan, key)
        if key == "unsupported_reason":
            assert expected_value in actual
        else:
            assert actual == expected_value


@pytest.mark.parametrize(
    ("query", "expected_category"),
    [
        ("鼠标", "mouse"),
        ("游戏鼠标", "mouse"),
        ("机械键盘", "keyboard"),
        ("耳麦", "headset"),
        ("头戴耳机", "headset"),
        ("屏幕", "monitor"),
        ("音响", "speaker"),
        ("蓝牙音箱", "speaker"),
        ("网络摄像头", "webcam"),
    ],
)
def test_catalog_category_aliases_are_canonicalized(
    query: str, expected_category: str
) -> None:
    plan = validate_product_query_plan(ProductQueryPlan(query=query, category=query))

    assert plan.category == expected_category


def test_catalog_product_plan_to_search_request_preserves_structured_filters() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="recommend a wireless Logitech mouse under 300",
            category="mouse",
            brands=["Logitech"],
            max_price=Decimal("300"),
            filters={"connection_type": "Wireless"},
            planner="llm",
            limit=3,
        )
    )

    request = _plan_to_product_search(plan)

    assert request.category == "mouse"
    assert request.max_price == Decimal("300")
    assert request.filters == {"connection_type": "Wireless"}
    assert request.limit == 3
    assert "Logitech" in request.query
    assert "recommend" not in request.query.lower()


def test_llm_product_plan_avoids_overconstrained_model_keyword_prefilter() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="144Hz 2K monitor",
            category="monitor",
            filters={"refresh_rate": "144Hz", "resolution": "2560x1440"},
            keywords=["144Hz", "2K"],
            planner="llm",
            limit=3,
        )
    )

    request = _plan_to_product_search(plan)

    assert request.query == ""
    assert request.category == "monitor"
    assert request.filters == {"refresh_rate": "144Hz", "resolution": "2560x1440"}


def test_llm_product_plan_avoids_numeric_spec_keyword_prefilter() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="30W bluetooth speaker",
            category="speaker",
            filters={"power_w": "30W", "connection_type": "bluetooth"},
            keywords=["speaker"],
            planner="llm",
            limit=3,
        )
    )

    request = _plan_to_product_search(plan)

    assert request.query == ""
    assert request.category == "speaker"
    assert request.filters == {"power_w": "30", "connection_type": "蓝牙"}


def test_product_plan_prunes_redundant_connection_value_from_type_filter() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="wireless headset with microphone",
            category="headset",
            filters={"type": "wireless", "microphone": "Yes", "connection_type": "Wireless"},
            planner="llm",
            limit=3,
        )
    )

    assert plan.filters == {"microphone": "Yes", "connection_type": "Wireless"}
    assert plan.normalization_debug["pruned_filters"] == [{"key": "type", "value": "wireless"}]


def test_rule_based_product_plan_avoids_overconstrained_sql_prefilter() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="144Hz 2K monitor",
            category="monitor",
            filters={"refresh_rate": "144Hz", "resolution": "2560x1440"},
            planner="rule_based",
            limit=3,
        )
    )

    request = _plan_to_product_search(plan)

    assert request.query == ""
    assert request.category == "monitor"
    assert request.filters == {"refresh_rate": "144Hz", "resolution": "2560x1440"}


def test_rule_based_product_plan_keeps_brand_as_safe_prefilter() -> None:
    plan = validate_product_query_plan(
        ProductQueryPlan(
            query="wireless mouse under 300 from Logitech",
            category="mouse",
            brands=["Logitech"],
            max_price=Decimal("300"),
            filters={"connection_type": "Wireless"},
            planner="rule_based",
            limit=3,
        )
    )

    request = _plan_to_product_search(plan)

    assert request.query == "Logitech"
    assert request.category == "mouse"
    assert request.max_price == Decimal("300")
    assert request.filters == {"connection_type": "Wireless"}


def test_catalog_product_plan_rejects_sql_injection_like_filter_key() -> None:
    with pytest.raises(ValueError, match="unsupported catalog filters"):
        validate_product_query_plan(
            ProductQueryPlan(
                query="find mouse",
                category="mouse",
                filters={"sku.price); drop table sku; --": "1"},
            )
        )


@pytest.mark.parametrize(
    ("specs", "key", "expected"),
    [
        ({"connection_type": "蓝牙"}, "connection_type", "Wireless"),
        ({"connection_type": "2.4G 无线"}, "connection_type", "Wireless"),
        ({"connection_type": "三模"}, "connection_type", "Wireless"),
        ({"connection_type": "USB-C"}, "connection_type", "Wired"),
        ({"connection_type": "USB-C"}, "connection_type", "USB-C"),
        ({"connection_type": "USB-A"}, "connection_type", "USB-A"),
        ({"connection_type": "蓝牙"}, "connection_type", "蓝牙"),
        ({"connection_type": "2.4G 无线"}, "connection_type", "2.4G 无线"),
        ({"connection_type": "三模"}, "connection_type", "三模"),
        ({"connection_type": "USB-A"}, "connection_type", "USB"),
        ({"connection_type": "USB-C"}, "connection_type", "USB"),
        ({"connection_type": "Bluetooth"}, "connection_type", "无线"),
        ({"connection_type": "有线连接"}, "connection_type", "Wired"),
        ({"wireless": "是"}, "wireless", "true"),
        ({"switches": "线性红轴"}, "switches", "Red"),
        ({"switches": "静音红轴"}, "switches", "Red"),
        ({"switches": "段落茶轴"}, "switches", "Brown"),
        ({"color": "黑色"}, "color", "Black"),
        ({"resolution": "2K"}, "resolution", "2560x1440"),
        ({"refresh_rate": "144 赫兹"}, "refresh_rate", "144Hz"),
        ({"refresh_rate": "75Hz"}, "refresh_rate", "75Hz"),
        ({"microphone": "带麦"}, "microphone", "Yes"),
        ({"microphone": "是"}, "microphone", "Yes"),
        ({"backlit": "无背光"}, "backlit", "No"),
        ({"backlit": "白光"}, "backlit", "Yes"),
    ],
)
def test_catalog_db_value_aliases_match_mixed_language_specs(
    specs: dict[str, str], key: str, expected: str
) -> None:
    assert _matches_single_filter(specs, key, expected)


def test_catalog_db_value_aliases_do_not_cross_match_unrelated_values() -> None:
    assert not _matches_single_filter(
        {"connection_type": "有线连接"}, "connection_type", "Wireless"
    )
    assert not _matches_single_filter(
        {"connection_type": "USB-A"}, "connection_type", "USB-C"
    )
    assert not _matches_single_filter(
        {"connection_type": "USB-C"}, "connection_type", "USB-A"
    )
    assert not _matches_single_filter(
        {"connection_type": "2.4G 无线"}, "connection_type", "蓝牙"
    )
    assert not _matches_single_filter(
        {"connection_type": "有线"}, "connection_type", "USB"
    )
    assert not _matches_single_filter({"switches": "青轴"}, "switches", "Red")


def test_catalog_search_sql_contains_expected_joins_filters_and_ordering() -> None:
    request = ProductSearchRequest(
        query="Logitech",
        category="mouse",
        min_price=Decimal("100"),
        max_price=Decimal("300"),
        excluded_brands=["Razer"],
        limit=3,
    )

    compiled = _sql(_catalog_search_statement(request, limit=3, offset=0))

    assert "join spu" in compiled
    assert "join brand" in compiled
    assert "join category" in compiled
    assert "sku.status = 1" in compiled
    assert "spu.status = 1" in compiled
    assert "sku.price >= 100" in compiled
    assert "sku.price <= 300" in compiled
    assert "brand.name not ilike" in compiled
    assert "category.name ilike" in compiled
    assert "order by sku.sales_count desc, sku.stock desc, sku.price asc" in compiled
    assert "limit 3" in compiled


def test_catalog_search_sql_does_not_embed_post_filter_specs_as_raw_sql() -> None:
    request = ProductSearchRequest(
        query="",
        category="monitor",
        filters={"refresh_rate": "144Hz", "resolution": "2560x1440"},
        limit=5,
    )

    compiled = _sql(_catalog_search_statement(request, limit=5, offset=0))

    assert "144hz" not in compiled
    assert "2560x1440" not in compiled
    assert "goods_attribute_relation" not in compiled
    assert "limit 5" in compiled
