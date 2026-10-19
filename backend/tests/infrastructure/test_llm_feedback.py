"""Three-category feedback and real validation loops, without network or database fixtures."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Literal

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.goal_planning import (
    ExecutionPlan,
    GoalPlan,
    InputBinding,
    error_feedback,
    generate_plan,
    validate_execution,
)
from app.agent.routing import RequestRoutePlan, RoutedSubquery
from app.core.llm_calls import ModelCallError
from app.core.llm_feedback import (
    OutputErrorType,
    OutputValidationError,
    validation_feedback,
)
from app.schemas.catalog_search import CatalogSearchRequest, SearchCondition
from app.tools.catalog import LLMCatalogQueryPlanner, ProductQueryPlan
from app.tools.contracts import DefaultToolContractProvider
from app.tools.schemas import CatalogFacetInput
from tests.support.tool_plan_fixtures import facet_plan


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(strict=True, ge=1, le=5)
    kind: Literal["one", "many"]


class Envelope(BaseModel):
    items: list[Item]


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


@pytest.mark.parametrize(
    ("schema", "data", "path"),
    [
        (InputBinding, {"source": "task_output", "ref": "task_1", "kind": "product",
                        "positions": [1, 1]}, ["positions"]),
        (ProductQueryPlan, {"query": "查询价格", "min_price": 100, "max_price": 50}, ["min_price"]),
    ],
)
def test_pydantic_wrapped_business_rules_keep_their_explicit_category(schema, data, path):
    with pytest.raises(ValidationError) as caught:
        schema.model_validate(data)
    feedback = validation_feedback(caught.value)
    assert feedback["errors"][0]["type"] == "RULE_ERROR"
    assert feedback["errors"][0]["path"] == path
    json.dumps(feedback)


def test_nested_business_rule_keeps_parent_location():
    data = execution_data()
    data["tasks"][0]["bindings"] = [{
        "source": "task_output", "ref": "task_2", "kind": "product", "positions": [1, 1],
    }]
    with pytest.raises(ValidationError) as caught:
        ExecutionPlan.model_validate(data)
    error = validation_feedback(caught.value)["errors"][0]
    assert error["type"] == "RULE_ERROR"
    assert error["path"] == ["tasks", 0, "bindings", 0, "positions"]


def test_uncategorized_program_errors_do_not_become_model_repair_feedback():
    with pytest.raises(TypeError):
        validation_feedback(ValueError("program defect"))
    error = error_feedback(ValueError("program defect"))["errors"][0]
    assert error["code"] == "execution_error"
    assert "type" not in error and "repair_instruction" not in error


class SequenceModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return output


def execution_data():
    return {"tasks": [{"goal_id": "goal_1", "id": "task_1", "tool_name": "catalog_search",
                       "arguments": {"query": "查询键盘", "limit": 2}}]}


def execution_call(data, name="submit_execution_plan"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": data, "id": "plan"}])


def execution_validator(plan):
    route = RequestRoutePlan(rewritten_query="查询键盘", subqueries=[
        RoutedSubquery(id="goal_1", query="查询键盘", disposition="tool_planning",
                       reason_code="test", planning_pending=True),
    ])
    validate_execution(plan, route, DefaultToolContractProvider(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing_call", "wrong_name", "schema", "duplicate", "cycle"])
async def test_real_execution_validation_repairs_each_category_once(defect):
    bad = execution_data()
    if defect == "schema":
        bad["tasks"] = {}
    elif defect == "duplicate":
        bad["tasks"].append(deepcopy(bad["tasks"][0]))
    elif defect == "cycle":
        bad["tasks"][0]["depends_on"] = ["task_2"]
        other = deepcopy(bad["tasks"][0])
        other.update(id="task_2", depends_on=["task_1"])
        bad["tasks"].append(other)
    first = (
        AIMessage(content="no call") if defect == "missing_call"
        else execution_call(bad, "wrong_function" if defect == "wrong_name"
                            else "submit_execution_plan")
    )
    model = SequenceModel([first, execution_call(execution_data())])
    payload = {"query": "查询键盘", "requested_limit": 2}
    snapshot = deepcopy(payload)
    validations = []

    def validate(plan):
        validations.append(plan)
        execution_validator(plan)

    plan = await generate_plan(
        model, "submit_execution_plan", ExecutionPlan, "test", payload, validate, lambda: None,
    )
    assert len(model.calls) == 2 and payload == snapshot
    assert plan.tasks[0].arguments == {"query": "查询键盘", "limit": 2}
    error = json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]
    expected = ("FORMAT_ERROR" if defect in {"missing_call", "wrong_name"}
                else "SCHEMA_ERROR" if defect == "schema" else "RULE_ERROR")
    assert error["type"] == expected and error["repair_instruction"]
    assert len(validations) == (2 if expected == "RULE_ERROR" else 1)
    assert model.calls[1][1].content == model.calls[0][1].content


@pytest.mark.asyncio
async def test_valid_same_query_tasks_are_not_rejected_as_semantic_duplicates():
    data = execution_data()
    data["tasks"].append({**deepcopy(data["tasks"][0]), "id": "task_2"})
    model = SequenceModel([execution_call(data)])
    plan = await generate_plan(
        model, "submit_execution_plan", ExecutionPlan, "test", {},
        execution_validator, lambda: None,
    )
    assert len(model.calls) == 1 and len(plan.tasks) == 2


@pytest.mark.asyncio
async def test_llm1_duplicate_goal_rule_is_not_mislabeled_as_schema_error():
    goal = {"id": "goal_1", "query": "查询键盘", "source_text": "查询键盘",
            "disposition": "tool_planning"}
    valid = {"rewritten_query": "查询键盘", "goals": [goal]}
    bad = {**valid, "goals": [goal, goal]}
    model = SequenceModel([execution_call(bad, "route_request"),
                           execution_call(valid, "route_request")])
    result = await generate_plan(
        model, "route_request", GoalPlan, "test", {}, lambda _: None, lambda: None,
    )
    assert len(result.goals) == 1 and len(model.calls) == 2
    error = json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]
    assert error["type"] == "RULE_ERROR" and error["path"] == ["goals"]


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


@pytest.mark.parametrize(
    ("arguments", "category", "path"),
    [
        ({"query": "查询键盘", "limit": "2"}, "SCHEMA_ERROR", ["limit"]),
        ({"query": "查询键盘", "scope": {"sku_ids": [1], "spu_ids": [2]}},
         "RULE_ERROR", ["scope"]),
        ({"query": "查询键盘", "scope": {}}, "RULE_ERROR", ["scope"]),
    ],
)
def test_tool_arguments_keep_full_task_path_and_rule_category(arguments, category, path):
    data = execution_data()
    data["tasks"][0]["arguments"] = arguments
    with pytest.raises(ValidationError) as caught:
        execution_validator(ExecutionPlan.model_validate(data))
    error = validation_feedback(caught.value)["errors"][0]
    assert error["type"] == category
    assert error["path"] == ["tasks", 0, "arguments", *path]


def test_existing_public_and_internal_rule_contracts_still_raise_validation_errors():
    with pytest.raises(ValidationError):
        CatalogSearchRequest(query="查询", scope={})
    with pytest.raises(ValidationError) as caught:
        SearchCondition(key="color", operator="eq", values=["白", "黑"], evidence="白或黑")
    error = validation_feedback(caught.value)["errors"][0]
    assert error["type"] == "RULE_ERROR" and error["path"] == ["values"]
