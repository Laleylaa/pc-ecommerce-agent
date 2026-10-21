
import pytest
from app.schemas.chat import ChatRequest
from pydantic import ValidationError

from evals.legacy.eval_goal_planning import assess
from tests.support.orchestration_goal_planning_validation import (
    plan_data,
)


@pytest.mark.parametrize("message", [None, 123, {"query": "键盘"}])
def test_invalid_chat_inputs_rejected(message):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"message": message})


def test_whitespace_only_input_is_left_for_backend_rules():
    assert ChatRequest(message=" \n\t ").message == " \n\t "


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
