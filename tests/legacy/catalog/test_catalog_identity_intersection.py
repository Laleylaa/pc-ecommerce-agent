import json

import pytest
from app.agent.legacy.answer_context import build_answer_context

from tests.support.answer_state import result_state
from tests.support.catalog_catalog_identity_intersection import (
    invoke,
    plan_for,
)
from tests.support.tool_planning import product


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_conflicting_name_and_real_id_clarifies_without_broadening(monkeypatch, tool):
    other = product(2, 2, color="黑色")
    other.product.title = other.product.spu_title = "K02"
    result, model, repository = await invoke(
        monkeypatch, tool, "查询K02（SPU 1）的价格及规格",
        [product(1, color="白色"), other], plan_for(tool, name="K02"),
    )
    assert result["ok"], result
    output = result["output"]
    assert output["diagnostics"][0]["code"] == "identity_mismatch"
    assert not output.get("products") and not output.get("items")
    assert len(model.calls) == 1
    assert len(repository.calls) == 1 and repository.calls[0][1].spu_ids == [1]
    context = build_answer_context(result_state(tool, output, dependency=True))
    assert context["tasks"][0]["handling"] == "clarify"
    assert context["tasks"][0]["result"]["reason_code"] == "identity_mismatch"
    assert context["tasks"][1]["resolved"] is False
    standalone = build_answer_context(result_state(tool, output))
    assert standalone["recommended_control_action"] == "ask_clarification"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["catalog_search", "catalog_facets"])
async def test_actual_smoke_identity_query_through_formal_graph(monkeypatch, tool):
    from tests.support.tool_chain import chain

    name = "Pulsar 云梭 M08 游戏鼠标"
    row = product(373, 32, category="鼠标", price="252", color="黑色")
    row.product.title = f"{name} 黑色标准版"
    row.product.spu_title = name
    row.product.brand = "Pulsar"
    query = f"查询 {name}（SPU 32）的价格及规格"
    result, model, _, _ = await chain(
        monkeypatch, query, tool, [plan_for(tool, name=name, identity=32)], [row],
        arguments={"scope": {"spu_ids": [32]}},
    )
    assert result["ok"], result
    assert len(model.calls) == 1
    first_input = model.calls[0][1].content
    assert name in first_input and "\\u" not in first_input
    assert json.loads(first_input)["query"] == query
    output = result["output"]
    assert output.get("products") or output.get("items")
    assert not any(d["code"] in {"multiple_targets", "identity_mismatch"}
                   for d in output["diagnostics"])
