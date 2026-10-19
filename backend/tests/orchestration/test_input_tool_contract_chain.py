import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from langgraph.errors import NodeCancelledError
from sqlalchemy.dialects import sqlite

from tests.support.tool_chain import (
    chain,
    feedback,
)
from tests.support.tool_plan_fixtures import facet_plan, order_plan, search_plan
from tests.support.tool_planning import product


@pytest.mark.asyncio
@pytest.mark.parametrize("soft", [False, True])
async def test_chain_hard_conditions_and_soft_ranking(monkeypatch, soft):
    query = (
        "键盘200元以内，白色优先，其他颜色也可以" if soft
        else "想买白色蓝牙键盘，200元以内"
    )
    conditions = [
        {"key": "price", "operator": "lte", "values": ["200"], "evidence": "200元以内"},
        {"key": "color", "values": ["白色"], "evidence": "白色"},
    ]
    if soft:
        conditions[1].update(
            strength="preference", preference_evidence="白色优先，其他颜色也可以",
        )
    else:
        conditions.append(
            {"key": "connection_type", "values": ["蓝牙"], "evidence": "蓝牙"}
        )
    rows = [
        product(1, 1, price="190", color="白色", connection_type="蓝牙"),
        product(2, 2, price="100", color="黑色", connection_type="蓝牙"),
        product(3, 3, price="300", color="白色", connection_type="蓝牙"),
        product(4, 2, price="150", color="白色", connection_type="有线"),
    ]
    result, model, repository, calls = await chain(
        monkeypatch, query, "catalog_search",
        [search_plan({"category": "键盘", "conditions": conditions})], rows,
    )
    output = result["output"]
    assert result["ok"] and output["status"] == "success"
    assert output["total_match_count"] == (2 if soft else 1)
    assert {sku for p in output["products"] for sku in p["candidate_sku_ids"]} == (
        {1, 2, 4} if soft else {1}
    )
    assert output["match_kind"] == "exact"
    assert len(model.calls) == len(calls) == 1
    assert repository.calls
    assert not repository.calls[0][1].sku_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["SKU", "SPU"])
@pytest.mark.parametrize("data", ["complete", "empty", "absent", "partial"])
async def test_chain_object_spec_status_and_granularity(monkeypatch, kind, data):
    rows = [] if data == "absent" else [product(1, style="75%")]
    if data == "empty":
        rows = [product(1)]
    if data == "partial":
        rows.append(product(2))
    result, model, repository, _ = await chain(
        monkeypatch, f"{kind}1有什么规格", "catalog_facets", [], rows,
    )
    output = result["output"]
    assert result["ok"] and not model.calls
    scope = repository.calls[0][1]
    assert getattr(scope, f"{kind.lower()}_ids") == [1]
    missing = data == "empty" or (data == "partial" and kind == "SPU")
    codes = {d["code"] for d in output["diagnostics"]}
    assert output["matching_complete"] is not missing
    assert output["is_exhaustive"] is not missing
    if missing:
        assert "data_insufficient" in codes and "not_found" not in codes
        assert output["field_issues"]
    elif data == "absent":
        assert codes == {"target_not_found"} and output["items"] == []
    else:
        assert output["items"][0]["values"][0]["sku_ids"] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["explicit", "remaining"])
@pytest.mark.parametrize("repair", [False, True])
async def test_chain_order_rule_feedback_is_bounded(monkeypatch, selection, repair):
    invalid = order_plan({"selection": selection})
    valid = order_plan({"unresolved_requirements": ["核对时间条件"]})
    result, model, repository, calls = await chain(
        monkeypatch, "去年有哪些订单", "order_lookup", [invalid, valid if repair else invalid],
    )
    assert len(model.calls) == 2 and len(calls) == 1 and not repository.calls
    assert feedback(model)["type"] == "RULE_ERROR"
    assert feedback(model)["path"] == ["selection"]
    if repair:
        assert result["output"]["result_type"] == "unsupported"
        assert not result["output"]["query_plan"]["fully_supported"]
    else:
        assert not result["ok"] and result["error"]["code"] == "planning_output_invalid"
        assert result["error"]["recommended_action"] == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("invalid_identity", ["both", "bare_target", "wrong_kind"])
async def test_chain_facet_identity_cannot_bypass_source_validation(
    monkeypatch, repair, invalid_identity,
):
    query = "请告诉我SPU1的键盘配列有哪些"
    bad = {"kind": "sku", "id": 9999 if invalid_identity == "both" else 1}
    query = "键盘里1的配列有哪些" if invalid_identity == "bare_target" else query
    invalid = facet_plan({
        "selection": "object", "facet": "spec_value", "spec_key": "style",
        "category": "keyboard", "target": bad,
        "name": "K01" if invalid_identity == "both" else None,
    })
    valid = facet_plan({
        "selection": "object", "facet": "spec_key",
        "supported": False, "unsupported_reason": "请明确对象",
    })
    result, model, repository, _ = await chain(
        monkeypatch, query, "catalog_facets", [invalid, valid if repair else invalid],
        [product(9999, 2, style="75%")],
    )
    assert not repository.calls
    # A bare number does not authorize the model to invent a SKU identity;
    # repair the plan first, then return the model's valid business limitation.
    assert len(model.calls) == 2 and feedback(model)["type"] == "RULE_ERROR"
    if repair:
        assert result["output"]["diagnostics"][0]["code"] == "query_needs_clarification"
    else:
        assert result["error"]["code"] == "planning_output_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,kind", [("not json", "FORMAT_ERROR"), ({}, "SCHEMA_ERROR")])
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets", "order_lookup"])
async def test_chain_invalid_internal_output_never_becomes_a_query(monkeypatch, raw, kind, tool):
    result, model, repository, calls = await chain(
        monkeypatch, "帮我按条件查一下去年买的键盘", tool, [raw, raw],
    )
    assert not result["ok"] and result["error"]["code"] == "planning_output_invalid"
    assert not repository.calls and len(calls) == 1 and len(model.calls) == 2
    assert feedback(model)["type"] == kind


@pytest.mark.asyncio
async def test_chain_context_scope_and_soft_category_hint(monkeypatch):
    result, model, repository, calls = await chain(
        monkeypatch, "这款有什么规格", "catalog_facets", [],
        [product(1, style="75%"), product(2, 2, style="全尺寸")],
        memory={"catalog": {"candidate_sku_ids": [1]}}, refs=["product_1"],
        bindings=[{"source": "context", "ref": "product_1", "kind": "product"}],
        arguments={"scope": {"sku_ids": [1]}, "category_hint": "鼠标"},
    )
    assert result["output"]["items"][0]["values"][0]["sku_ids"] == [1]
    assert calls[0][0]["scope"] == {"sku_ids": [1], "spu_ids": []}
    assert repository.calls[0][0] is None and not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("copied_target", [False, True])
async def test_chain_facet_scope_aggregation_and_copied_singleton(monkeypatch, copied_target):
    ids = [1] if copied_target else [1, 2]
    query = "只看SKU1的配列有哪些" if copied_target else "只看SKU1和SKU2的配列有哪些"
    plan = facet_plan({"facet": "spec_value", "spec_key": "style"})
    if copied_target:
        # Trusted context scope permits a redundant target without fabricating an identity.
        query = "这款的配列有哪些"
        plan["target"] = {"kind": "sku", "id": 1}
    result, model, repository, _ = await chain(
        monkeypatch, query, "catalog_facets", [plan],
        [product(1, style="75%"), product(2, style="全尺寸"), product(3, style="60%")],
        arguments={"scope": {"sku_ids": ids}},
        memory={"catalog": {"candidate_sku_ids": [1]}} if copied_target else None,
        refs=["product_1"] if copied_target else None,
        bindings=[{"source": "context", "ref": "product_1", "kind": "product"}]
        if copied_target else None,
    )
    output = result["output"]
    assert {item["value"] for item in output["items"]} == (
        {"75%"} if copied_target else {"75%", "全尺寸"}
    )
    assert output["matching_complete"] and len(model.calls) == 1
    assert repository.calls[0][1].sku_ids == ids
    if copied_target:
        assert output["query_plan"]["target"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["clarify", "unsupported", "conflicting"])
async def test_chain_business_feedback_does_not_query_or_repair(monkeypatch, operation):
    query = "办公键盘有哪些配列"
    plan = facet_plan({"facet": "spec_value", "spec_key": "style"})
    code = "requirement_needs_clarification"
    if operation == "clarify":
        plan["clarification_requirements"] = ["办公"]
    elif operation == "unsupported":
        query = "键盘按香味筛选有哪些配列"
        plan.update(supported=False, unsupported_requirements=["按香味筛选"])
        code = "request_not_supported"
    else:
        query = "价格至少300元但至多200元的键盘有哪些配列"
        plan = facet_plan({"conditions": [
            {"key": "price", "operator": "gte", "values": ["300"], "evidence": "至少300元"},
            {"key": "price", "operator": "lte", "values": ["200"], "evidence": "至多200元"},
        ]})
        code = "conflicting_conditions"
    result, model, repository, _ = await chain(
        monkeypatch, query, "catalog_facets", [plan], [product(1, style="75%")],
    )
    output = result["output"]
    assert output["diagnostics"][0]["code"] == code
    assert not output["matching_complete"] and not output["is_exhaustive"]
    assert len(model.calls) == 1 and not repository.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("time_field", ["created_at", "pay_at", "delivery_at"])
async def test_chain_order_filters_reach_real_count_sql(monkeypatch, time_field):
    database = sqlite3.connect(":memory:")
    database.executescript("""
        CREATE TABLE order_info (id INTEGER, user_id INTEGER, status INTEGER,
            created_at TEXT, pay_at TEXT, delivery_at TEXT);
        INSERT INTO order_info (id,user_id,status,created_at) VALUES
            (1,7,4,'2025-01-01 00:00:00.000000'),
            (2,7,4,'2026-01-01 00:00:00.000000'),
            (3,7,3,'2025-02-01 00:00:00.000000'),
            (4,8,4,'2025-02-01 00:00:00.000000'),
            (5,7,4,'2024-12-31 23:59:59.999999');
        UPDATE order_info SET pay_at=created_at, delivery_at=created_at;
    """)
    reads = []

    class Session:
        async def execute(self, statement):
            compiled = statement.compile(dialect=sqlite.dialect())
            reads.append(compiled.params)
            # Execute the production statement on isolated SQLite data.
            sql = statement.compile(
                dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True},
            )
            value = database.execute(str(sql)).fetchone()[0]
            return SimpleNamespace(scalar_one=lambda: value)

    filters = {
        "time_field": time_field, "start_at": "2025-01-01T00:00:00+08:00",
        "end_at": "2026-01-01T00:00:00+08:00", "statuses": [4], "product_terms": [],
    }
    dimension = {"created_at": "下单", "pay_at": "支付", "delivery_at": "发货"}[time_field]
    try:
        result, model, _, _ = await chain(
            monkeypatch, f"2025年{dimension}且已完成的订单有几笔", "order_lookup",
            [order_plan({"operation": "total_count", "requested_fields": ["order_count"],
                         "filters": filters, "filter_evidence": ["2025年", "已完成"]})],
            session=Session(),
        )
    finally:
        database.close()
    output = result["output"]
    assert output["result_type"] == "order_count" and output["total_match_count"] == 1
    assert output["query_plan"]["filters"]["time_field"] == time_field
    assert output["query_plan"]["fully_supported"]
    assert len(reads) == len(model.calls) == 1
    assert reads[0]["user_id_1"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets", "order_lookup"])
@pytest.mark.parametrize("failure", ["timeout", "runtime", "cancel"])
async def test_chain_internal_timeout_exception_and_cancel_boundaries(monkeypatch, tool, failure):
    error = {"timeout": TimeoutError, "runtime": RuntimeError, "cancel": asyncio.CancelledError}
    call = chain(
        monkeypatch, "帮我按条件查一下去年买的键盘", tool,
        [error[failure]("synthetic") for _ in range(2)],
    )
    if failure == "cancel":
        with pytest.raises(NodeCancelledError) as caught:
            await call
        assert isinstance(caught.value.__cause__, asyncio.CancelledError)
        return
    result, model, repository, calls = await call
    assert not repository.calls and len(calls) == 1
    assert len(model.calls) == (2 if failure == "timeout" else 1)
    if failure == "timeout":
        assert result["error"]["code"] == "planning_call_timeout"
        assert not result["error"]["retryable"]
    elif tool == "order_lookup":
        assert result["output"]["result_type"] == "planning_failed"
    else:
        assert not result["ok"]
