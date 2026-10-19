"""Offline adversarial plan checks; no real models, sessions, or repositories."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.agent.goal_planning import ExecutionPlan, GoalPlan, validate_execution
from app.agent.routing import RequestRoutePlan, RoutedSubquery
from app.schemas.chat import ChatRequest
from app.tools.contracts import DefaultToolContractProvider
from scripts.eval_goal_planning import assess


def plan_data():
    return {
        "tasks": [
            {
                "goal_id": "goal_1",
                "id": "task_1",
                "tool_name": "catalog_search",
                "arguments": {"query": "查询键盘"},
            }
        ]
    }


@pytest.mark.parametrize(
    "defect",
    [
        "unknown_tool",
        "invalid_argument",
        "identity_override",
        "invented_scope",
        "missing_goal",
        "duplicate_task",
        "self_dependency",
        "cycle",
        "unknown_context",
        "wrong_dependency_kind",
        "no_user_facing_task",
    ],
)
def test_invalid_plans_are_rejected_before_execution(defect):
    data = plan_data()
    task = data["tasks"][0]
    if defect == "unknown_tool":
        task["tool_name"] = "delete_orders"
    elif defect == "invalid_argument":
        task["arguments"]["limit"] = -1
    elif defect == "identity_override":
        task["arguments"]["user_id"] = 999
    elif defect == "invented_scope":
        task["arguments"]["scope"] = {"sku_ids": [999]}
    elif defect == "missing_goal":
        task["goal_id"] = "goal_2"
    elif defect == "duplicate_task":
        data["tasks"].append(deepcopy(task))
    elif defect == "self_dependency":
        task["depends_on"] = ["task_1"]
    elif defect == "cycle":
        task["depends_on"] = ["task_2"]
        data["tasks"].append({**deepcopy(task), "id": "task_2", "depends_on": ["task_1"]})
    elif defect == "unknown_context":
        task["bindings"] = [{"source": "context", "ref": "product_99", "kind": "product"}]
    elif defect == "wrong_dependency_kind":
        data["tasks"].append(
            {
                "id": "task_2",
                "goal_id": "goal_1",
                "tool_name": "order_lookup",
                "arguments": {"query": "最近订单"},
            }
        )
        task.update(
            depends_on=["task_2"],
            bindings=[
                {
                    "source": "task_output",
                    "ref": "task_2",
                    "kind": "product",
                }
            ],
        )
    elif defect == "no_user_facing_task":
        task["answer_role"] = "internal"
    route = RequestRoutePlan(
        rewritten_query="查询键盘",
        subqueries=[
            RoutedSubquery(
                id="goal_1",
                query="查询键盘",
                disposition="tool_planning",
                planning_pending=True,
                reason_code="test_goal",
            )
        ],
    )
    with pytest.raises((ValidationError, ValueError)):
        validate_execution(
            ExecutionPlan.model_validate(data), route, DefaultToolContractProvider(), {}
        )


@pytest.mark.parametrize("message", [None, 123, {"query": "键盘"}])
def test_invalid_chat_inputs_rejected(message):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": message})


def test_whitespace_only_input_is_left_for_backend_rules():
    assert ChatRequest(message=" \n\t ").message == " \n\t "


def test_invalid_goal_type_and_removed_unresolved_field_rejected():
    with pytest.raises(ValidationError):
        GoalPlan.model_validate(
            {
                "rewritten_query": "键盘",
                "goals": [
                    {
                        "id": "goal_1",
                        "query": "键盘",
                        "source_text": "键盘",
                        "disposition": "execute_sql",
                    }
                ],
            }
        )
    data = plan_data()
    data["unresolved_requirements"] = []
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(data)


def test_live_evaluator_detects_office_misrouting_even_if_runtime_guard_corrects_it():
    case = {
        "id": "office_test",
        "message": "办公室键盘",
        "dispositions": ["tool_planning"],
        "tools": ["catalog_search"],
    }
    state = {
        "goal_plan": {"goals": [{"query": "办公室键盘", "disposition": "clarification"}]},
        "route_plan": {"subqueries": [{"query": "办公室键盘", "disposition": "tool_planning"}]},
        "execution_plan": plan_data(),
    }
    assert any("llm1: dispositions" in failure for failure in assess(case, state))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["llm1", "llm2"])
@pytest.mark.parametrize("repeat_failure", [False, True])
async def test_shared_json_repair_returns_raw_error_and_stops_after_two_attempts(
    stage, repeat_failure
):
    import json

    from langchain_core.messages import AIMessage

    from app.agent.goal_planning import generate_plan

    name = "route_request" if stage == "llm1" else "submit_execution_plan"
    schema = GoalPlan if stage == "llm1" else ExecutionPlan
    valid = (
        {
            "rewritten_query": "你好",
            "goals": [
                {
                    "id": "goal_1",
                    "query": "你好",
                    "source_text": "你好",
                    "disposition": "direct_response",
                }
            ],
        }
        if stage == "llm1"
        else plan_data()
    )
    malformed = json.dumps(valid, ensure_ascii=False) + "}"
    try:
        json.loads(malformed)
    except json.JSONDecodeError as error:
        parser_error = str(error)

    class Model:
        def __init__(self):
            self.messages = []

        async def ainvoke(self, messages):
            self.messages.append(list(messages))
            if len(self.messages) == 1 or repeat_failure:
                return AIMessage(
                    content="",
                    invalid_tool_calls=[
                        {
                            "name": name,
                            "args": malformed,
                            "id": "bad",
                            "error": parser_error,
                        }
                    ],
                )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": valid,
                        "id": "good",
                    }
                ],
            )

    attempts = []
    validated = []
    model = Model()

    async def run():
        return await generate_plan(
            model,
            name,
            schema,
            "test",
            {},
            validated.append,
            lambda: attempts.append(1),
        )

    if repeat_failure:
        with pytest.raises(ValueError, match="not valid JSON"):
            await run()
        assert not validated
    else:
        assert isinstance(await run(), schema)
        assert len(validated) == 1
    assert len(model.messages) == len(attempts) == 2
    feedback = json.loads(model.messages[1][-1].content)
    assert feedback["previous_output"]["invalid_tool_calls"][0]["args"] == malformed
    errors = feedback["validation_feedback"]["errors"]
    assert len(errors) == 1
    assert errors[0]["type"] == "FORMAT_ERROR"
    assert errors[0]["path"] == ["invalid_tool_calls", 0, "args"]
    assert errors[0]["code"] == "invalid_json"
    assert errors[0]["message"] == parser_error
    assert "响应形式" in errors[0]["repair_instruction"]
    assert name in feedback["repair_instruction"]
    assert "完整合法的 JSON" in feedback["repair_instruction"]
    assert all(message.type in {"system", "human"} for message in model.messages[1])


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_calls_cannot_partially_pass_validation():
    import json

    from langchain_core.messages import AIMessage

    from app.agent.goal_planning import generate_plan

    messages_seen = []
    validated = []

    class Model:
        async def ainvoke(self, messages):
            messages_seen.append(list(messages))
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_execution_plan",
                        "args": plan_data(),
                        "id": "good",
                    }
                ],
                invalid_tool_calls=[
                    {
                        "name": "submit_execution_plan",
                        "args": "{",
                        "id": "bad",
                        "error": None,
                    }
                ],
            )

    with pytest.raises(ValueError, match="not valid JSON"):
        await generate_plan(
            Model(),
            "submit_execution_plan",
            ExecutionPlan,
            "test",
            {},
            validated.append,
            lambda: None,
        )
    assert len(messages_seen) == 2 and not validated
    feedback = json.loads(messages_seen[1][-1].content)
    assert feedback["previous_output"]["tool_calls"][0]["args"] == plan_data()
    assert feedback["validation_feedback"]["errors"][0]["message"] == "JSON format is invalid"


@pytest.mark.parametrize("mutation", [None, "untrusted", "conflict", "old_scope", "empty_scope"])
def test_historical_goal_identity_requires_source_without_goal_scope(mutation):
    from app.agent.goal_planning import Goal, validate_goal_identity

    data = {
        "id": "goal_1",
        "query": "查询SKU 201的价格",
        "source_text": "第二款多少钱",
        "disposition": "tool_planning",
        "context_refs": ["product_2"],
    }
    objects = {"product_2": {"kind": "product", "entity_scope": "sku", "ids": [201]}}
    if mutation == "untrusted":
        data["context_refs"] = []
    elif mutation == "conflict":
        data["query"] = "查询SKU 202的价格"
    elif mutation == "old_scope":
        data["scope"] = {"sku_ids": [201]}
    elif mutation == "empty_scope":
        data["scope"] = {}
    if mutation:
        with pytest.raises(ValueError):
            validate_goal_identity(Goal.model_validate(data), "第二款多少钱", objects)
    else:
        validate_goal_identity(Goal.model_validate(data), "第二款多少钱", objects)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("查SKU编号：101和SPU编号20", {("sku", 101), ("spu", 20)}),
        ("查sku #101的库存", {("sku", 101)}),
        ("查第二款和型号K101", set()),
        ("型号ASKU101、SKU101A、SKU101.5", set()),
        ("编号1有什么规格", {("sku", 1)}),
        ("查商品编号：999的价格", {("sku", 999)}),
        ("SPU编号1的规格", {("spu", 1)}),
        ("查订单编号1和物流编号2", set()),
        ("预算999元，型号K999，第1款", set()),
    ],
)
def test_product_identity_has_one_shared_rule_and_does_not_guess(text, expected):
    from app.agent.goal_planning import explicit_product_ids as planning_ids
    from app.rules.product_identity import explicit_product_ids
    from app.tools.catalog_search.validation import explicit_product_ids as search_ids

    assert planning_ids is search_ids is explicit_product_ids
    assert explicit_product_ids(text) == expected


@pytest.mark.parametrize(
    "query",
    [
        "查SKU编号999", "查SKU编号：999", "查SPU编号999",
        "查编号999", "查商品编号999", "查商品编号：999",
    ],
)
def test_alternative_id_spellings_do_not_bypass_source_validation(query):
    from app.agent.goal_planning import Goal, validate_goal_identity

    goal = Goal(
        id="goal_1", query=query, source_text="查询SKU编号101", disposition="tool_planning",
    )
    with pytest.raises(ValueError, match="product IDs need explicit input"):
        validate_goal_identity(goal, goal.source_text, {})


@pytest.mark.parametrize("mutation", [None, "outside", "scope_only", "binding_conflict"])
def test_direct_task_scope_is_validated_against_goal(mutation):
    from app.agent.goal_planning import bind_arguments

    route = RequestRoutePlan(
        rewritten_query="查SKU 201",
        subqueries=[
            RoutedSubquery(
                id="goal_1",
                query="查询SKU 201的价格",
                disposition="tool_planning",
                reason_code="test",
                planning_pending=True,
            )
        ],
    )
    data = plan_data()
    args = data["tasks"][0]["arguments"]
    args.update(query="查SKU 201价格", scope={"sku_ids": [201]})
    if mutation == "outside":
        args["scope"] = {"sku_ids": [999]}
    elif mutation == "scope_only":
        args["query"] = "查价格"
    elif mutation == "binding_conflict":
        data["tasks"][0]["bindings"] = [
            {"source": "context", "ref": "product_2", "kind": "product"}
        ]
    plan = ExecutionPlan.model_validate(data)
    if mutation in {"outside", "binding_conflict"}:
        with pytest.raises(ValueError):
            validate_execution(plan, route, DefaultToolContractProvider(), {})
    else:
        validate_execution(plan, route, DefaultToolContractProvider(), {})
        assert bind_arguments({}, plan.tasks[0], plan.tasks[0].arguments)["scope"]["sku_ids"] == [
            201
        ]


@pytest.mark.parametrize("mutation", [
    None, "overlap", "missing", "empty", "whitespace", "unicode_space",
    "too_long", "max_length", "all_tasks",
])
def test_flat_plan_preserves_blocked_goal_coverage(mutation):
    route = RequestRoutePlan(
        rewritten_query="两个目标",
        subqueries=[
            RoutedSubquery(
                id=f"goal_{i}",
                query="查询键盘",
                disposition="tool_planning",
                reason_code="test",
                planning_pending=True,
            )
            for i in (1, 2)
        ],
    )
    data = plan_data()
    data["blocked_goals"] = [{"goal_id": "goal_2", "blocked_reason": "缺少必要对象"}]
    if mutation == "overlap":
        data["blocked_goals"][0]["goal_id"] = "goal_1"
    elif mutation == "missing":
        data["blocked_goals"] = []
    elif mutation in {"empty", "whitespace", "unicode_space", "too_long", "max_length"}:
        data["blocked_goals"][0]["blocked_reason"] = {
            "empty": "", "whitespace": " \n\t", "unicode_space": "\u3000\u00a0",
            "too_long": "缺" * 501, "max_length": "缺" * 500,
        }[mutation]
    elif mutation == "all_tasks":
        data["blocked_goals"] = []
        data["tasks"].append({**deepcopy(data["tasks"][0]), "id": "task_2", "goal_id": "goal_2"})
    if mutation not in {None, "max_length", "all_tasks"}:
        with pytest.raises(ValueError):
            validate_execution(
                ExecutionPlan.model_validate(data), route, DefaultToolContractProvider(), {}
            )
    else:
        plan = ExecutionPlan.model_validate(data)
        validate_execution(
            plan, route, DefaultToolContractProvider(), {}
        )
        assert [g.model_dump() for g in plan.blocked_goals] == data["blocked_goals"]


@pytest.mark.parametrize("index", range(12))
def test_prompt_examples_obey_formal_tool_contracts_and_goal_coverage(index):
    from app.agent.prompts.planning_examples import TASK_PLANNING_EXAMPLES

    example = TASK_PLANNING_EXAMPLES[index]
    route = RequestRoutePlan(
        rewritten_query="；".join(g["query"] for g in example["input"]["goals"]),
        subqueries=[
            RoutedSubquery(
                **g,
                disposition="tool_planning",
                reason_code="example",
                planning_pending=True,
            )
            for g in example["input"]["goals"]
        ],
    )
    plan = ExecutionPlan.model_validate(example["output"])
    validate_execution(plan, route, DefaultToolContractProvider(), {})
    if index == 6:
        assert {t.tool_name for t in plan.tasks} == {"catalog_search", "catalog_facets"}
        assert all(not t.depends_on and t.answer_role == "user_facing" for t in plan.tasks)
