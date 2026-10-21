
import json
from copy import deepcopy

import pytest
from app.agent.legacy.goal_planning import (
    ExecutionPlan,
    GoalPlan,
    InputBinding,
    error_feedback,
    generate_plan,
)
from app.core.llm_feedback import (
    validation_feedback,
)
from app.tools.catalog import ProductQueryPlan
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from tests.support.infrastructure_llm_feedback import (
    SequenceModel,
    execution_call,
    execution_data,
    execution_validator,
)


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
