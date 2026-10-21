import asyncio
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from app.core.llm_calls import ModelCallError
from app.models import OrderInfo
from app.repositories.catalog_search import CatalogSearchRepository
from app.repositories.orders import OrderRepository, OrderSearchPage, _order_conditions
from app.schemas.catalog_search import CatalogSearchRequest, FieldIssue, SearchScope
from app.schemas.order import OrderFilters
from app.tools.catalog import (
    CatalogToolService,
    LLMCatalogQueryPlanner,
    RuleBasedCatalogQueryPlanner,
)
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.orders import LLMOrderQueryPlanner, OrderQueryPlan, OrderToolService
from app.tools.schemas import (
    CatalogFacetInput,
    CatalogFacetOutput,
    OrderLookupInput,
    ToolDiagnostic,
)
from openai import APIStatusError
from sqlalchemy import func, select
from sqlalchemy.dialects import sqlite

from tests.support.tool_plan_fixtures import facet_plan, order_plan, search_plan
from tests.support.tool_planning import (
    Model,
    Repository,
    conditions,
    product,
)


@pytest.mark.asyncio
async def test_empty_search_repair_preserves_all_conditions_and_filters_same_sku():
    model = Model({}, search_plan({"category": "键盘", "conditions": conditions()}))
    repository = Repository(
        [
            product(1, color="白色", connection_type="蓝牙"),
            product(2, color="白色", connection_type="蓝牙", price="599"),
            product(3, color="黑色", connection_type="蓝牙"),
            product(4, color="白色", connection_type="有线"),
        ]
    )
    result = await CatalogSearchService(repository, CatalogSearchPlanner(model)).search(
        CatalogSearchRequest(query="白色蓝牙键盘200元以内")
    )
    assert result.products[0].candidate_sku_ids == [1]
    assert result.total_match_count == 1 and len(model.calls) == 2
    feedback = json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]
    assert feedback["type"] == "SCHEMA_ERROR" and feedback["code"] == "missing"
    assert model.calls[0][1].content == model.calls[1][1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets", "order_lookup"])
async def test_empty_model_exhaustion_is_terminal_and_never_queries(tool, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid planning must not reach a repository")

    monkeypatch.setattr(CatalogSearchRepository, "load", forbidden)
    monkeypatch.setattr(OrderRepository, "count_orders", forbidden)
    model = Model({}, {})
    if tool == "catalog_search":
        handler = CatalogSearchService(Repository(), CatalogSearchPlanner(model)).search
    elif tool == "catalog_facets":
        handler = CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets
    else:
        handler = OrderToolService(None, planner=LLMOrderQueryPlanner(model)).lookup
    result = await execute_bound_tool(
        BoundTool(DefaultToolContractProvider().get_contract(tool), handler),
        {"query": "白色蓝牙键盘有哪些布局" if tool != "order_lookup" else "去年有几笔订单"},
        {"user_id": 7},
    )
    assert not result.ok and result.error.code == "planning_output_invalid"
    assert not result.error.retryable and result.error.recommended_action == "stop"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_nested_condition_missing_operator_is_not_completed_by_default():
    valid = search_plan({"conditions": conditions()})
    invalid = search_plan({"conditions": conditions()})
    invalid["conditions"][2].pop("operator")
    model = Model(invalid, valid)
    await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query="白色蓝牙键盘200元以内"))
    issue = json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]
    assert issue["type"] == "SCHEMA_ERROR" and issue["path"] == ["conditions", 2, "operator"]


@pytest.mark.asyncio
async def test_boolean_rule_error_repairs_instead_of_becoming_empty_search():
    invalid = search_plan(
        {"conditions": [{"key": "microphone", "values": ["带这个功能"], "evidence": "带麦克风"}]}
    )
    valid = search_plan(
        {"conditions": [{"key": "microphone", "values": ["是"], "evidence": "带麦克风"}]}
    )
    model = Model(invalid, valid)
    plan = await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query="找带麦克风的摄像头"))
    assert plan.conditions[0].values == ["是"]
    assert (
        json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]["type"]
        == "RULE_ERROR"
    )


@pytest.mark.asyncio
async def test_transport_retries_same_messages_then_repairs_with_one_shared_attempt_limit():
    response = httpx.Response(
        429, headers={"retry-after": "0"}, request=httpx.Request("POST", "https://model.invalid")
    )
    error = APIStatusError("limited", response=response, body=None)
    model = Model(error, {}, search_plan())
    await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query="找键盘"))
    assert len(model.calls) == 3
    assert [m.content for m in model.calls[0]] == [m.content for m in model.calls[1]]
    assert len(model.calls[2]) == 3


@pytest.mark.asyncio
async def test_rejected_model_request_is_not_an_output_repair():
    response = httpx.Response(403, request=httpx.Request("POST", "https://model.invalid"))
    model = Model(APIStatusError("rejected", response=response, body=None))
    with pytest.raises(ModelCallError) as caught:
        await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query="找键盘"))
    assert caught.value.code == "planning_model_rejected" and len(model.calls) == 1


@pytest.mark.asyncio
async def test_facet_typo_is_repaired_and_style_has_shared_category_guidance():
    valid = facet_plan(
        {"facet": "spec_value", "category": "键盘", "spec_key": "style", "conditions": conditions()}
    )
    invalid = dict(valid)
    invalid["filterz"] = invalid.pop("filters")
    model = Model(invalid, valid)
    plan = await LLMCatalogQueryPlanner(model).plan_facets(
        CatalogFacetInput(
            query="白色蓝牙键盘200元以内有哪些布局可选",
            category_hint="键盘",
        )
    )
    assert len(plan.conditions) == 3 and plan.spec_key == "style"
    assert "键盘配列" in model.calls[0][0].content
    assert json.loads(model.calls[0][1].content)["explicit_overrides"] == {}


@pytest.mark.asyncio
async def test_model_facet_brand_cannot_bypass_original_evidence_validation():
    invalid = facet_plan({"category": "键盘", "spec_key": "color", "brand": "Razer"})
    valid = facet_plan(
        {
            "category": "键盘",
            "spec_key": "color",
            "conditions": [{"key": "brand", "values": ["Logitech"], "evidence": "罗技"}],
        }
    )
    model = Model(invalid, valid)
    plan = await LLMCatalogQueryPlanner(model).plan_facets(
        CatalogFacetInput(query="罗技键盘有哪些颜色")
    )
    assert plan.brand is None and plan.conditions[0].values == ["Logitech"]
    feedback = json.loads(model.calls[1][-1].content)["validation_feedback"]
    assert feedback["errors"][0]["type"] == "RULE_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,kind,expected_skus",
    [
        ("编号1有什么规格", "sku", {1}),
        ("商品编号：1有什么规格", "sku", {1}),
        ("SKU编号1有什么规格", "sku", {1}),
        ("SPU编号1有什么规格", "spu", {1, 2}),
    ],
)
async def test_number_label_defaults_to_sku_without_rewriting_query_or_involving_llm(
    monkeypatch,
    query,
    kind,
    expected_skus,
):
    records = [product(1, style="75%"), product(2, style="全尺寸")]
    repository = Repository(records)

    async def load(session, category, scope):
        return await repository.load(category, scope)

    monkeypatch.setattr(CatalogSearchRepository, "load", load)
    model = Model()  # Calling the model would fail: complete grammar needs no LLM.
    result = await CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets(
        CatalogFacetInput(query=query)
    )
    assert result.query_plan["target"] == {"kind": kind, "id": 1}
    assert result.query_plan["query"] == query and model.calls == []
    assert {
        sku for item in result.items for value in item.values for sku in value.sku_ids
    } == expected_skus


@pytest.mark.asyncio
async def test_source_valid_missing_number_returns_not_found_instead_of_planning_failure(
    monkeypatch,
):
    repository = Repository([product(1, style="75%")])

    async def load(session, category, scope):
        return await repository.load(category, scope)

    monkeypatch.setattr(CatalogSearchRepository, "load", load)
    service = CatalogToolService(None, planner=LLMCatalogQueryPlanner(Model()))
    result = await execute_bound_tool(
        BoundTool(DefaultToolContractProvider().get_contract("catalog_facets"), service.facets),
        {"query": "编号999有什么规格"},
        {"user_id": 7},
    )
    assert result.ok and result.output["result_type"] == "empty"
    assert [d["code"] for d in result.output["diagnostics"]] == ["target_not_found"]
    assert repository.calls[0][1].sku_ids == [999]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,wrong,correct",
    [
        ("请告诉我编号1的键盘配列有哪些", {"kind": "spu", "id": 1}, {"kind": "sku", "id": 1}),
        ("请告诉我SPU1的键盘配列有哪些", {"kind": "sku", "id": 1}, {"kind": "spu", "id": 1}),
    ],
)
async def test_tool_model_uses_the_number_default_and_cannot_change_explicit_spu(
    query,
    wrong,
    correct,
):
    base = {"selection": "object", "facet": "spec_value", "spec_key": "style", "category": "键盘"}
    model = Model(facet_plan(base | {"target": wrong}), facet_plan(base | {"target": correct}))
    plan = await LLMCatalogQueryPlanner(model).plan_facets(CatalogFacetInput(query=query))
    assert plan.target.model_dump() == correct and len(model.calls) == 2
    assert (
        json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]["type"]
        == "RULE_ERROR"
    )


@pytest.mark.asyncio
async def test_catalog_search_number_alias_is_a_sku_lookup_with_no_model():
    plan = await CatalogSearchPlanner().plan(CatalogSearchRequest(query="编号1的价格"))
    assert plan.target.model_dump() == {"kind": "sku", "id": 1}
    assert plan.requested_fields == ["price"]


@pytest.mark.asyncio
async def test_facets_use_numeric_equality_and_do_not_hide_conflicts(monkeypatch):
    rows = [
        product(1, category="显示器", refresh_rate="144Hz", size_inch="24英寸"),
        product(2, category="显示器", refresh_rate="44Hz", size_inch="4英寸"),
    ]
    rows[0].issues = [
        FieldIssue(sku_id=1, field="color", status="conflict", values=["白色", "黑色"])
    ]
    repository = Repository(rows)

    async def load(_, category, scope):
        return await repository.load(category, scope)

    monkeypatch.setattr(CatalogSearchRepository, "load", load)
    model = Model(
        facet_plan(
            {
                "category": "显示器",
                "facet": "spec_key",
                "conditions": [
                    {"key": "refresh_rate", "values": ["44Hz"], "evidence": "44Hz"},
                    {"key": "size_inch", "values": ["4英寸"], "evidence": "4英寸"},
                ],
            }
        )
    )
    result = await CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets(
        CatalogFacetInput(query="44Hz、4英寸显示器有哪些规格")
    )
    assert result.items and all(item.sku_count == 1 for item in result.items)
    assert {sku for item in result.items for v in item.values for sku in v.sku_ids} == {2}


@pytest.mark.asyncio
async def test_object_facets_scope_keeps_sku_associations_and_conflict_dimension(monkeypatch):
    rows = [
        product(1, color="白色", style="87键"),
        product(2, color="黑色", style="104键"),
        product(3, spu=2, color="粉色", style="75%"),
    ]
    rows[1].issues = [
        FieldIssue(sku_id=2, field="style", status="conflict", values=["104键", "87键"])
    ]
    repository = Repository(rows)

    async def load(_, category, scope):
        return await repository.load(category, scope)

    monkeypatch.setattr(CatalogSearchRepository, "load", load)
    result = await CatalogToolService(None).facets(
        CatalogFacetInput(
            query="这款有什么规格",
            scope=SearchScope(spu_ids=[1]),
        )
    )
    colors = next(item for item in result.items if item.value == "color")
    assert {v.value: v.sku_ids for v in colors.values} == {"白色": [1], "黑色": [2]}
    style = next(item for item in result.items if item.value == "style")
    assert [(v.value, v.sku_ids) for v in style.values] == [("87键", [1])]
    assert result.field_issues[0].sku_id == 2 and not result.is_exhaustive


@pytest.mark.asyncio
async def test_object_without_identity_does_not_expand_to_whole_catalog():
    from app.tools.catalog import FacetQueryPlan
    from app.tools.catalog_facets import execute_facets

    repository = Repository()
    result = await execute_facets(
        repository,
        CatalogFacetInput(query="这款有什么规格"),
        FacetQueryPlan(selection="object", facet="spec_key"),
    )
    assert repository.calls == []
    assert result.diagnostics[0].code == "query_needs_clarification"


@pytest.mark.asyncio
async def test_unconfigured_facet_planner_does_not_drop_open_language_conditions():
    with pytest.raises(OSError):
        await RuleBasedCatalogQueryPlanner().plan_facets(
            CatalogFacetInput(
                query="白色蓝牙键盘200元以内有哪些布局可选",
            )
        )


def test_sql_filters_use_half_open_range_user_scope_and_same_order_item():
    # This database exists only in memory; no configured engine or repository fixture.
    database = sqlite3.connect(":memory:")
    database.executescript("""
        CREATE TABLE order_info (id INTEGER, user_id INTEGER, status INTEGER,
            created_at TEXT, pay_at TEXT, delivery_at TEXT);
        CREATE TABLE order_item (id INTEGER, order_id INTEGER, sku_name TEXT, sku_specs TEXT);
        INSERT INTO order_info VALUES
            (1,7,4,'2025-01-01 00:00:00.000000',NULL,NULL),
            (2,7,3,'2025-06-01 00:00:00.000000',NULL,NULL),
            (3,7,4,'2026-01-01 00:00:00.000000',NULL,NULL),
            (4,8,4,'2025-06-01 00:00:00.000000',NULL,NULL),
            (5,7,4,'2025-06-01 00:00:00.000000',NULL,NULL);
        INSERT INTO order_item VALUES (1,1,'白色键盘','{}'),(2,2,'白色键盘','{}'),
            (3,3,'白色键盘','{}'),(4,4,'白色键盘','{}'),
            (5,5,'白色鼠标','{}'),(6,5,'黑色键盘','{}');
    """)
    filters = OrderFilters(
        start_at=datetime(2025, 1, 1),
        end_at=datetime(2026, 1, 1),
        statuses=[4],
        product_terms=["白色", "键盘"],
    )
    where = _order_conditions(7, filters)
    for statement, expected in [
        (select(OrderInfo.id).where(*where), [(1,)]),
        (select(func.count(OrderInfo.id)).where(*where), [(1,)]),
    ]:
        sql = str(
            statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
        )
        # SQLite's json_each supplies the same key/value table for this isolated fixture.
        # Production uses PostgreSQL JSONB; only the table-function name is adapted.
        sql = sql.replace("jsonb_each_text(", "json_each(")
        assert database.execute(sql).fetchall() == expected
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,selection", [("total_count", "all"), ("list", "all"), ("detail", "latest")]
)
async def test_order_execution_passes_one_filter_to_count_list_and_latest(
    operation, selection, monkeypatch
):
    calls = []

    class Orders:
        def __init__(self, session):
            pass

        async def count_orders(self, user, *, filters):
            calls.append(("count", user, filters))
            return 1

        async def list_recent_orders_page(self, user, *, filters, limit, offset):
            calls.append(("list", user, filters))
            return OrderSearchPage([], 1, offset)

        async def latest_order(self, user, *, filters):
            calls.append(("latest", user, filters))
            return None

    monkeypatch.setattr("app.tools.orders.OrderRepository", Orders)
    filters = {
        "time_field": "created_at",
        "start_at": "2025-01-01T00:00:00",
        "end_at": "2026-01-01T00:00:00",
        "statuses": [4],
        "product_terms": [],
    }
    model = Model(
        order_plan(
            {
                "selection": selection,
                "operation": operation,
                "filters": filters,
                "filter_evidence": ["去年", "已完成"],
            }
        )
    )
    result = await OrderToolService(None, planner=LLMOrderQueryPlanner(model)).lookup(
        OrderLookupInput(
            user_id=7,
            query="去年已完成的订单",
            limit=1,
        )
    )
    assert result.result_type != "planning_failed" and calls
    assert all(user == 7 and f.model_dump(mode="json") == filters for _, user, f in calls)
    payload = json.loads(model.calls[0][1].content)
    assert payload["timezone"] == "Asia/Shanghai" and "current_time" in payload
    assert "user_id" not in payload


@pytest.mark.asyncio
async def test_unresolved_order_filter_stops_before_repository_creation(monkeypatch):
    def forbidden(*args):
        raise AssertionError("Unresolved filters must not expand to all orders")

    monkeypatch.setattr("app.tools.orders.OrderRepository", forbidden)
    model = Model(order_plan({"unresolved_requirements": ["按预计送达时间筛选"]}))
    result = await OrderToolService(None, planner=LLMOrderQueryPlanner(model)).lookup(
        OrderLookupInput(
            user_id=7,
            query="列出预计明天送达的订单",
        )
    )
    assert result.result_type == "unsupported" and not result.query_plan["fully_supported"]
    assert result.query_plan["unresolved_requirements"] == ["按预计送达时间筛选"]


@pytest.mark.asyncio
async def test_output_serialization_failure_is_a_terminal_execution_error():
    async def handler(request):
        return CatalogFacetOutput(
            result_type="empty",
            facet="brand",
            diagnostics=[
                ToolDiagnostic(
                    code="test",
                    message="test",
                    details={"not_json": object()},
                )
            ],
        )

    result = await execute_bound_tool(
        BoundTool(DefaultToolContractProvider().get_contract("catalog_facets"), handler),
        {"query": "有哪些品牌"},
        {"user_id": 7},
    )
    assert not result.ok and result.error.code == "execution_error"
    assert not result.error.retryable and result.error.recommended_action == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"product_terms": ["   "]}, {"start_at": 1735689600}])
async def test_invalid_order_filter_values_are_repaired_before_execution(invalid):
    filters = order_plan()["filters"] | invalid
    model = Model(order_plan({"filters": filters}), order_plan())
    await LLMOrderQueryPlanner(model).plan(OrderLookupInput(user_id=7, query="查询我的订单"))
    feedback = json.loads(model.calls[1][-1].content)["validation_feedback"]
    assert feedback["errors"][0]["type"] == "SCHEMA_ERROR"
    assert feedback["errors"][0]["path"][0] == "filters"


@pytest.mark.asyncio
async def test_late_model_response_is_rejected_even_if_cancellation_was_suppressed():
    from app.tools.structured_planning import generate_tool_plan

    class LateModel:
        async def ainvoke(self, messages):
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass
            return SimpleNamespace(content=json.dumps(order_plan()))

    with pytest.raises(ModelCallError) as caught:
        await generate_tool_plan(
            LateModel(),
            schema=OrderQueryPlan,
            required=OrderQueryPlan.model_required_fields,
            prompt="test",
            payload={},
            timeout_seconds=0.001,
        )
    assert caught.value.code == "planning_call_timeout"


@pytest.mark.asyncio
async def test_repository_preserves_json_relation_and_duplicate_attribute_conflicts():
    sku = SimpleNamespace(
        id=1,
        title="K01",
        spu_id=1,
        status=1,
        price="100",
        stock=1,
        sales_count=1,
        image_url=None,
        specs_json={"layout": "87键", "color": "White"},
    )
    spu = SimpleNamespace(id=1, title="K01", status=1, sales_count=1, detail_html="")

    class Session:
        calls = 0

        async def execute(self, statement):
            self.calls += 1
            rows = (
                [(sku, spu, SimpleNamespace(name="Test"), SimpleNamespace(name="键盘"))]
                if self.calls == 1
                else [(1, "style", "104键"), (1, "布局", "75%"), (1, "颜色", "白色")]
            )
            return SimpleNamespace(all=lambda: rows)

    records = await CatalogSearchRepository(Session()).load(None, SearchScope())
    assert records[0].product.specs["color"] == "White"
    assert [(issue.field, issue.values) for issue in records[0].issues] == [
        ("style", ["87键", "104键", "75%"]),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        '{"operation":"search","conditions":[],"conditions":[{"key":"color"}]}',
        '{"min_price":NaN}',
        "```json\n{}\n```",
    ],
)
async def test_ambiguous_or_non_json_output_enters_format_repair(raw):
    model = Model(raw, search_plan())
    await CatalogSearchPlanner(model).plan(CatalogSearchRequest(query="找键盘"))
    error = json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]
    assert error["type"] == "FORMAT_ERROR"


@pytest.mark.asyncio
async def test_conflicting_order_time_range_is_not_unsupported_or_an_unfiltered_query(monkeypatch):
    def forbidden(*args):
        raise AssertionError("Conflicting ranges must not query")

    monkeypatch.setattr("app.tools.orders.OrderRepository", forbidden)
    model = Model(
        order_plan(
            {
                "filters": {
                    "time_field": "created_at",
                    "start_at": "2026-01-01T00:00:00",
                    "end_at": "2025-01-01T00:00:00",
                    "statuses": [],
                    "product_terms": [],
                },
                "filter_evidence": ["2026年1月到2025年1月"],
            }
        )
    )
    result = await OrderToolService(None, planner=LLMOrderQueryPlanner(model)).lookup(
        OrderLookupInput(user_id=7, query="查询2026年1月到2025年1月的订单"),
    )
    assert result.result_type == "invalid_query"
    assert result.diagnostics[0].code == "conflicting_conditions"
    assert len(model.calls) == 1


def test_order_product_terms_search_values_and_aliases_without_matching_json_keys():
    database = sqlite3.connect(":memory:")
    database.executescript("""
        CREATE TABLE order_info (id INTEGER, user_id INTEGER, status INTEGER,
            created_at TEXT, pay_at TEXT, delivery_at TEXT);
        CREATE TABLE order_item (id INTEGER, order_id INTEGER, sku_name TEXT, sku_specs TEXT);
        INSERT INTO order_info VALUES (1,7,4,NULL,NULL,NULL),(2,7,4,NULL,NULL,NULL);
        INSERT INTO order_item VALUES
            (1,1,'Razer mouse','{"color":"White","wireless":false}'),
            (2,2,'Keyboard','{"color":"Black","wireless":false}');
    """)
    for terms, expected in [(["雷蛇", "鼠标", "白色"], [(1,)]), (["wireless"], [])]:
        statement = select(OrderInfo.id).where(
            *_order_conditions(7, OrderFilters(product_terms=terms))
        )
        sql = str(
            statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert (
            database.execute(sql.replace("jsonb_each_text(", "json_each(")).fetchall() == expected
        )
    database.close()
