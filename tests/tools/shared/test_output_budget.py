"""Output boundaries through formal bindings; synthetic populations, zero external IO."""

import json

import pytest
from app.tools.contracts import BoundTool, default_tool_contracts, execute_bound_tool
from app.tools.output_budget import OutputBudgetExceeded, prepare_tool_output
from app.tools.output_limits import output_cost
from pydantic import ValidationError

from tests.support.output_budget_fixtures import (
    catalog_output,
    document_output,
    facets_output,
    order_output,
)

NAMES = [
    "catalog_search",
    "catalog_lookup",
    "catalog_facets",
    "order_lookup",
    "policy_search",
    "knowledge_search",
]


def contract(name, budget=12000):
    return next(c for c in default_tool_contracts() if c.name == name).model_copy(
        update={"output_token_budget": budget}
    )


async def execute(name, output, *, budget=12000, limit=3):
    calls = []

    async def handler(request):
        calls.append(request)
        return output

    result = await execute_bound_tool(
        BoundTool(contract(name, budget), handler),
        {"query": "合成预算请求", "limit": limit},
        {"user_id": 41},
    )
    assert len(calls) == 1
    assert (
        len(json.dumps(result.model_dump(mode="json"), ensure_ascii=False).encode())
        <= budget
    )
    return result


@pytest.mark.contract
@pytest.mark.parametrize("name", NAMES)
def test_schema_and_model_description_have_same_limit_and_budget(name):
    c = contract(name)
    maximum = 5 if name == "order_lookup" else 10
    schema = c.as_llm_tool()["function"]
    assert schema["parameters"]["properties"]["limit"]["maximum"] == maximum
    assert f"1 and {maximum}" in schema["description"]
    assert "12000" in schema["description"] and "UTF-8" in schema["description"]
    c.public_input_model.model_validate({"query": "查询", "limit": maximum})


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "10", 100])
async def test_invalid_limit_never_reaches_handler(name, bad):
    async def forbidden(request):
        pytest.fail("invalid input reached handler")

    result = await execute_bound_tool(
        BoundTool(contract(name), forbidden),
        {"query": "查询", "limit": bad},
        {"user_id": 41},
    )
    assert not result.ok and result.error.code == "invalid_input"


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_one_over_limit_rejected(name):
    async def forbidden(request):
        pytest.fail("over-limit input reached handler")

    result = await execute_bound_tool(
        BoundTool(contract(name), forbidden),
        {"query": "查询", "limit": 6 if name == "order_lookup" else 11},
        {"user_id": 41},
    )
    assert result.error.code == "invalid_input"


@pytest.mark.contract
@pytest.mark.parametrize(
    "value", ["英文", '"quoted"\\\n', "😀", {"中文": "字段", "list": [1, 2]}]
)
def test_cost_counts_serialized_utf8_including_json_and_unicode(value):
    assert output_cost(value) == len(
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    )


@pytest.mark.business
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "lookup"])
async def test_large_catalog_returns_complete_prefix_and_keeps_full_statistics(
    operation,
):
    original = catalog_output(operation, padding=100)
    before = original.model_dump(mode="json")
    result = await execute("catalog_" + operation, original, limit=10)
    assert result.ok, result.error
    out = result.output
    count = len(out["products"])
    assert 0 < count < 10 and out["returned_count"] == count
    assert out["products"] == before["products"][:count]
    assert out["total_match_count"] == 10 and out["matched_sku_count"] == 10
    assert out["matching_complete"] and not out["is_exhaustive"]
    assert out["next_offset"] == count
    assert out["output_budget"]["truncated"]
    if operation == "lookup":
        assert out["lookup_summary"] == before["lookup_summary"]
    assert original.model_dump(mode="json") == before


@pytest.mark.business
@pytest.mark.asyncio
async def test_facets_nested_previews_preserve_counts_and_source_provenance():
    result = await execute("catalog_facets", facets_output())
    assert result.ok, result.error
    item = result.output["items"][0]
    assert item["sku_count"] == 30 and item["spu_count"] == 3
    assert item["total_nested_value_count"] == 12 and not item["values_exhaustive"]
    assert len(item["values"]) == 10
    for v in item["values"]:
        assert v["sku_ids"] == list(range(1, 11)) and v["total_sku_ids_count"] == 30
        assert not v["sources_exhaustive"] and v["spu_ids"] == [1, 2, 3]
    assert result.output["total_value_count"] == 1


@pytest.mark.business
@pytest.mark.asyncio
async def test_lookup_version_sources_are_preview_not_population():
    original = catalog_output("lookup", count=12)
    data = original.model_dump(mode="json")
    data["lookup_summary"]["fields"] = [
        {
            "field": "color",
            "values": [
                {
                    "value": "黑色",
                    "versions": [
                        {"sku_id": i, "title": f"版本{i}"} for i in range(1, 13)
                    ],
                }
            ],
        }
    ]
    for product in data["products"]:
        product["specs"]["color"] = "黑色"
    original = type(original).model_validate(data)
    result = await execute("catalog_lookup", original, limit=10)
    assert result.ok, result.error
    value = result.output["lookup_summary"]["fields"][0]["values"][0]
    assert value["total_version_count"] == 12 and len(value["versions"]) == 10
    assert not value["versions_exhaustive"]


@pytest.mark.business
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["policy_search", "knowledge_search"])
async def test_retrieval_reduces_complete_documents_never_cuts_qualifications(name):
    original = document_output(name, length=650)
    result = await execute(name, original, budget=10000)
    assert result.ok, result.error
    docs = result.output["documents"]
    assert 0 < len(docs) < 3
    assert docs == original.model_dump(mode="json")["documents"][: len(docs)]
    assert all(d["snippet"].endswith("例外：定制品。") for d in docs)


@pytest.mark.business
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["analysis", "suggestions"])
async def test_order_windows_preserve_whole_orders_and_full_counts(mode):
    original = order_output(padding=800, mode=mode)
    result = await execute("order_lookup", original, budget=4500, limit=5)
    assert result.ok, result.error
    key = "analysis_orders" if mode == "analysis" else "suggestions"
    rows = result.output[key]
    assert 0 < len(rows) < 5
    assert rows == original.model_dump(mode="json")[key][: len(rows)]
    if mode == "analysis":
        assert result.output["total_match_count"] == 5
        assert result.output["focus_order_ids"] == [r["id"] for r in rows]
        assert not result.output["is_exhaustive"]
    else:
        assert result.output["suggestion_total_count"] == 5
        assert not result.output["suggestion_is_exhaustive"]


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["policy_search", "knowledge_search", "order_lookup"])
async def test_single_oversize_entry_is_explicit_error_not_empty_success(name):
    original = (
        order_output(count=1, padding=20000, mode="single")
        if name == "order_lookup"
        else document_output(name, count=1, length=20000)
    )
    result = await execute(name, original)
    assert not result.ok and result.output is None
    assert result.error.code == "output_budget_exceeded"
    assert (
        not result.error.retryable and result.error.recommended_action == "narrow_query"
    )


@pytest.mark.contract
@pytest.mark.asyncio
async def test_huge_validation_error_is_also_bounded():
    async def forbidden(request):
        pytest.fail("invalid input reached handler")

    result = await execute_bound_tool(
        BoundTool(contract("policy_search"), forbidden),
        {"query": ["隐私" * 20000]},
        {"user_id": 41},
    )
    assert not result.ok
    assert (
        len(json.dumps(result.model_dump(mode="json"), ensure_ascii=False).encode())
        <= 12000
    )


@pytest.mark.contract
def test_budget_threshold_includes_metadata_and_wrapper():
    original = document_output(count=1, length=50)
    first = prepare_tool_output("policy_search", original, 12000)
    wrapped = {
        "tool_name": "policy_search",
        "ok": True,
        "output": first.model_dump(mode="json"),
        "error": None,
    }
    # Decimal width of token_limit must be stable when constructing exact-fit boundary.
    budget = len(json.dumps(wrapped, ensure_ascii=False).encode())
    while True:
        wrapped["output"]["output_budget"]["token_limit"] = budget
        measured = len(json.dumps(wrapped, ensure_ascii=False).encode())
        if measured == budget:
            break
        budget = measured
    assert prepare_tool_output("policy_search", original, budget)
    with pytest.raises(OutputBudgetExceeded):
        prepare_tool_output("policy_search", original, budget - 1)


@pytest.mark.contract
@pytest.mark.parametrize("bad", [0, 16001])
def test_contract_rejects_invalid_budget_configuration(bad):
    with pytest.raises(ValidationError):
        type(contract("policy_search")).model_validate(
            contract("policy_search").model_dump() | {"output_token_budget": bad}
        )


@pytest.mark.smoke
@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_each_formal_tool_returns_small_bounded_result(name):
    if name.startswith("catalog_"):
        original = (
            facets_output(nested=2, sources=3)
            if name == "catalog_facets"
            else catalog_output(name.removeprefix("catalog_"), count=2)
        )
    else:
        original = (
            order_output(count=1)
            if name == "order_lookup"
            else document_output(name, count=1)
        )
    result = await execute(name, original, limit=2)
    assert result.ok, result.error
    assert not result.output["output_budget"]["truncated"]


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["catalog_search", "catalog_lookup", "catalog_facets"])
async def test_minimum_catalog_fact_set_too_large_is_not_partial_success(name):
    if name == "catalog_facets":
        original = facets_output(nested=1, sources=1)
        original.items[0].values[0].value = "完整值" * 20000
    else:
        original = catalog_output(name.removeprefix("catalog_"), count=1, padding=20000)
    result = await execute(name, original, limit=1)
    assert not result.ok and result.output is None
    assert result.error.code == "output_budget_exceeded"


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize("name", NAMES)
async def test_validation_trace_identifies_the_actual_delivered_output(
    monkeypatch, name
):
    from contextlib import contextmanager

    import app.tools.contracts as module

    captured = []

    @contextmanager
    def validation(*args, **kwargs):
        yield

    monkeypatch.setattr(module, "tracing_active", lambda: True)
    monkeypatch.setattr(module, "validation_step", validation)
    monkeypatch.setattr(
        module, "payload", lambda key, value: captured.append((key, value))
    )
    if name.startswith("catalog_"):
        original = (
            facets_output(nested=2, sources=3)
            if name == "catalog_facets"
            else catalog_output(name.removeprefix("catalog_"), count=2)
        )
    else:
        original = (
            order_output(count=1)
            if name == "order_lookup"
            else document_output(name, count=1)
        )
    result = await execute(name, original, limit=2)
    assert result.ok
    assert captured[-1] == ("input", result.output), (
        "validation evidence must match delivered output"
    )
