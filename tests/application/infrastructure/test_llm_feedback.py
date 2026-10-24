import asyncio
import json
from types import SimpleNamespace

import pytest
from app.core.llm_calls import ModelCallError
from app.core.llm_feedback import (
    OutputErrorType,
    OutputValidationError,
    validation_feedback,
)
from app.schemas.catalog_search import CatalogSearchRequest, SearchCondition
from app.tools.catalog.compatibility import LLMCatalogQueryPlanner
from app.tools.schemas import CatalogFacetInput
from pydantic import ValidationError

from tests.support.infrastructure_llm_feedback import (
    Envelope,
    SequenceModel,
)
from tests.support.tool_plan_fixtures import facet_plan


@pytest.mark.parametrize(
    ("raw", "expected_type", "path"),
    [
        ("{", "FORMAT_ERROR", []),
        ("[]", "SCHEMA_ERROR", []),
        ('{"items":{}}', "SCHEMA_ERROR", ["items"]),
        ('{"items":[{}]}', "SCHEMA_ERROR", ["items", 0, "count"]),
        ('{"items":[{"count":"2","kind":"one"}]}', "SCHEMA_ERROR", ["items", 0, "count"]),
        ('{"items":[{"count":6,"kind":"one"}]}', "SCHEMA_ERROR", ["items", 0, "count"]),
        ('{"items":[{"count":2,"kind":"wrong"}]}', "SCHEMA_ERROR", ["items", 0, "kind"]),
        ('{"items":[{"count":2,"kind":"one","extra":1}]}', "SCHEMA_ERROR",
         ["items", 0, "extra"]),
    ],
)
def test_native_errors_have_category_location_and_serializable_feedback(raw, expected_type, path):
    with pytest.raises(ValidationError) as caught:
        Envelope.model_validate_json(raw)
    feedback = validation_feedback(caught.value)
    assert feedback["errors"][0]["type"] == expected_type
    assert feedback["errors"][0]["path"] == path
    assert feedback["errors"][0]["message"]
    assert feedback["errors"][0]["repair_instruction"]
    assert json.loads(json.dumps(feedback)) == feedback
    assert all("input" not in error and "ctx" not in error for error in feedback["errors"])


def test_same_layer_reports_multiple_errors_without_exception_objects():
    with pytest.raises(ValidationError) as caught:
        Envelope.model_validate({"items": [{"count": 0, "kind": "wrong"}]})
    feedback = validation_feedback(caught.value)
    assert {tuple(error["path"]) for error in feedback["errors"]} == {
        ("items", 0, "count"), ("items", 0, "kind"),
    }
    assert all(error["type"] == "SCHEMA_ERROR" for error in feedback["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [("not JSON", "FORMAT_ERROR"), ("{", "FORMAT_ERROR"), ('{"facet":}', "FORMAT_ERROR"),
     ("[]", "SCHEMA_ERROR"), ("null", "SCHEMA_ERROR"),
     ('{"facet":"invalid"}', "SCHEMA_ERROR"), ('{"filters":[]}', "SCHEMA_ERROR")],
)
async def test_catalog_repair_preserves_raw_output_category_and_original_input(raw, expected_type):
    valid = facet_plan({"facet": "spec_value", "category": "键盘", "spec_key": "style",
                        "conditions": [
                            {"key":"color", "values":["白色"], "evidence":"白色"},
                            {"key":"connection_type", "values":["蓝牙"], "evidence":"蓝牙"},
                        ]})
    model = SequenceModel([SimpleNamespace(content=raw),
                           SimpleNamespace(content=json.dumps(valid, ensure_ascii=False)),
                           SimpleNamespace(content=json.dumps({
                               "verdict": "approve", "checks": [{
                                   "evidence": "白色蓝牙键盘有哪些布局可选",
                                   "plan_paths": ["/conditions"], "status": "matched",
                                   "explanation": "脚本审核通过，语义审核另有专项测试",
                               }],
                           }, ensure_ascii=False))])
    request = CatalogFacetInput(query="白色蓝牙键盘有哪些布局可选", limit=1)
    plan = await LLMCatalogQueryPlanner(model).plan_facets(request)
    assert len(model.calls) == 3
    payloads = [json.loads(call[1].content) for call in model.calls]
    assert payloads[2]["candidate_plan"]["conditions"] == valid["conditions"]
    feedback = json.loads(model.calls[1][-1].content)["validation_feedback"]
    assert json.loads(model.calls[1][-1].content)["previous_output"] == raw
    assert feedback["errors"][0]["type"] == expected_type
    assert payloads[1]["query"] == payloads[0]["query"] == request.query
    assert payloads[1]["explicit_overrides"] == payloads[0]["explicit_overrides"]
    assert plan.query == request.query and plan.limit == 1 and plan.facet == "spec_value"
    assert [(c.key, c.values) for c in plan.conditions] == [
        ("color", ["白色"]), ("connection_type", ["蓝牙"]),
    ]


@pytest.mark.asyncio
async def test_catalog_parse_failures_exhaust_repairs_without_stale_plan(monkeypatch):
    failures = []

    async def record(name, **data):
        if name == "tool_plan_validation_error":
            failures.append(data)

    monkeypatch.setattr("app.tools.structured_planning.trace_event", record)
    model = SequenceModel([SimpleNamespace(content='{"facet":"invalid","category":"mouse"}'),
                           SimpleNamespace(content="not JSON"), SimpleNamespace(content="{")])
    with pytest.raises(ModelCallError, match="after repair") as caught:
        await LLMCatalogQueryPlanner(model).plan_facets(CatalogFacetInput(query="查目录"))
    assert len(model.calls) == len(failures) == 3
    assert failures[0]["feedback"]["errors"][0]["type"] == "SCHEMA_ERROR"
    last = failures[-1]["feedback"]
    assert last["errors"][0]["type"] == "FORMAT_ERROR"
    assert [failure["will_repair"] for failure in failures] == [True, True, False]
    assert isinstance(caught.value.__cause__, OutputValidationError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [TimeoutError("timeout"), OSError("down"), asyncio.CancelledError()],
)
async def test_catalog_runtime_failures_do_not_trigger_output_repair(error):
    model = SequenceModel([error, error])
    with pytest.raises(ModelCallError if isinstance(error, TimeoutError) else type(error)):
        await LLMCatalogQueryPlanner(model).plan_facets(CatalogFacetInput(query="查目录"))
    assert len(model.calls) == (2 if isinstance(error, TimeoutError) else 1)


@pytest.mark.asyncio
async def test_catalog_unmarked_validator_bug_is_not_treated_as_rule_error():
    model = SequenceModel([SimpleNamespace(content='{"facet":"brand"}')])

    def broken(_):
        raise ValueError("program defect")

    with pytest.raises(ValueError, match="program defect"):
        await LLMCatalogQueryPlanner(model)._invoke_with_retry("test", {}, broken)
    assert len(model.calls) == 1


def test_feedback_has_exactly_three_public_categories():
    assert {value.value for value in OutputErrorType} == {
        "FORMAT_ERROR", "SCHEMA_ERROR", "RULE_ERROR",
    }


def test_existing_public_and_internal_rule_contracts_still_raise_validation_errors():
    with pytest.raises(ValidationError):
        CatalogSearchRequest(query="查询", scope={})
    with pytest.raises(ValidationError) as caught:
        SearchCondition(key="color", operator="eq", values=["白", "黑"], evidence="白或黑")
    error = validation_feedback(caught.value)["errors"][0]
    assert error["type"] == "RULE_ERROR" and error["path"] == ["values"]
