from typing import Any, Literal

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field

from app.agent.answer_contract import AnswerText
from app.agent.recovery import RecoveryInput

DecisionType = Literal[
    "recovery",
    "clarification",
    "grounded_response",
    "partial_response",
    "unavailable_response",
    "tool_calls",
    "invalid",
]

ControlAction = Literal[
    "ask_clarification",
    "finish_answer",
    "finish_partial",
    "finish_unavailable",
]


class PlannedToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    subquery: str = ""
    canonical_query: str = ""
    tool_query: str = ""


class OrchestratorDecision(BaseModel):
    type: DecisionType
    response: str = ""
    reason: str = ""
    tool_calls: list[PlannedToolCall] = Field(default_factory=list)
    control_action: ControlAction | None = None
    used_tool_call_ids: list[str] = Field(default_factory=list)
    unavailable_parts: list[str] = Field(default_factory=list)
    offer_handoff_confirmation: bool = False
    observation_coverage_failure: bool = False
    covered_goal_ids: list[str] = Field(default_factory=list)
    recovery_requests: list[dict[str, Any]] = Field(default_factory=list)


class _ControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: AnswerText
    covered_goal_ids: list[str] = Field(default_factory=list, max_length=8)


class AskClarificationInput(_ControlInput):
    missing_information: list[str] = Field(default_factory=list, max_length=5)


class FinishAnswerInput(_ControlInput):
    used_tool_call_ids: list[str] = Field(min_length=1, max_length=20)


class FinishPartialInput(FinishAnswerInput):
    unavailable_parts: list[str] = Field(min_length=1, max_length=10)
    offer_handoff_confirmation: bool = Field(
        default=False,
        description=(
            "True only when an unresolved part may require human handling and the user has "
            "not clearly requested handoff yet. Runtime renders the confirmation question."
        ),
    )


class FinishUnavailableInput(_ControlInput):
    unavailable_parts: list[str] = Field(min_length=1, max_length=10)
    observation_coverage_failure: bool = Field(
        default=False,
        description=(
            "True only when a successful Tool Observation does not contain the core facts "
            "requested by the user. Do not use it for empty results or transient failures."
        ),
    )
    offer_handoff_confirmation: bool = Field(
        default=False,
        description=(
            "True only when the unresolved request may require human handling and the user "
            "has not clearly requested handoff yet. Runtime renders the confirmation question."
        ),
    )


_CONTROL_MODELS: dict[str, type[BaseModel]] = {
    "request_recovery": RecoveryInput,
    "ask_clarification": AskClarificationInput,
    "finish_answer": FinishAnswerInput,
    "finish_partial": FinishPartialInput,
    "finish_unavailable": FinishUnavailableInput,
}

CONTROL_TOOL_NAMES = frozenset(_CONTROL_MODELS)

_CONTROL_DESCRIPTIONS = {
    "request_recovery": "Request one bounded batch of goal-understanding or tool-plan corrections.",
    "ask_clarification": (
        "Ask exactly one focused clarification when required information is missing."
    ),
    "finish_answer": (
        "Finish when every user-facing task has an answerable result, including reliable "
        "no-match results. Every listed tool call id must support one resolved task."
    ),
    "finish_partial": (
        "Finish with supported findings plus explicit unavailable parts for a mixed request."
    ),
    "finish_unavailable": (
        "Finish when tools ran but no usable information was available for the requested facts."
    ),
}


def control_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _CONTROL_DESCRIPTIONS[name],
                "parameters": model.model_json_schema(),
            },
        }
        for name, model in _CONTROL_MODELS.items()
    ]


def decision_from_ai_message(
    message: AIMessage,
) -> OrchestratorDecision:
    """Parse one native business-tool wave or one observation control action."""

    if message.invalid_tool_calls:
        raise ValueError("invalid tool calls cannot accompany an answer")
    if message.tool_calls:
        control_calls = [
            call for call in message.tool_calls if str(call.get("name")) in CONTROL_TOOL_NAMES
        ]
        if control_calls:
            if len(message.tool_calls) != 1:
                raise ValueError("a control action cannot be mixed with other tool calls")
            return _decision_from_control_call(control_calls[0])

        return OrchestratorDecision(
            type="tool_calls",
            tool_calls=[
                _planned_tool_call(call, index)
                for index, call in enumerate(message.tool_calls, start=1)
            ],
        )

    text = _message_content_to_text(message.content).strip()
    if text:
        raise ValueError("orchestrator returned plain text instead of a control action")
    raise ValueError("orchestrator returned neither tool calls nor a control action")


def _planned_tool_call(call: dict[str, Any], index: int) -> PlannedToolCall:
    name = str(call["name"])
    arguments = dict(call.get("args") or {})
    subquery = str(arguments.pop("subquery", "")).strip()
    return PlannedToolCall(
        id=str(call.get("id") or f"call_{index}"),
        name=name,
        arguments=arguments,
        subquery=subquery or infer_tool_subquery(name, arguments),
    )


def infer_tool_subquery(tool_name: str, arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query") or "").strip()
    if query:
        return query
    if tool_name == "order_lookup":
        order_id = arguments.get("order_id")
        return f"查询订单 {order_id}" if order_id is not None else "查询当前用户的最近订单"
    if tool_name == "catalog_facets":
        facet = str(arguments.get("facet") or "目录信息")
        category = str(arguments.get("category") or "").strip()
        return f"查询{category or '商品目录'}的{facet}"
    return f"使用 {tool_name} 查询所需信息"


def _decision_from_control_call(call: dict[str, Any]) -> OrchestratorDecision:
    name = str(call["name"])
    payload = _CONTROL_MODELS[name].model_validate(call.get("args") or {})
    data = payload.model_dump(mode="json")
    if name == "request_recovery":
        return OrchestratorDecision(type="recovery", recovery_requests=data["requests"])
    response = str(data["response"])

    if name == "ask_clarification":
        return OrchestratorDecision(
            type="clarification",
            response=response,
            covered_goal_ids=data["covered_goal_ids"],
            reason="missing_information",
            control_action=name,
            unavailable_parts=list(data["missing_information"]),
        )
    if name == "finish_answer":
        return OrchestratorDecision(
            type="grounded_response",
            response=response,
            covered_goal_ids=data["covered_goal_ids"],
            control_action=name,
            used_tool_call_ids=list(data["used_tool_call_ids"]),
        )
    if name == "finish_partial":
        return OrchestratorDecision(
            type="partial_response",
            response=response,
            covered_goal_ids=data["covered_goal_ids"],
            control_action=name,
            used_tool_call_ids=list(data["used_tool_call_ids"]),
            unavailable_parts=list(data["unavailable_parts"]),
            offer_handoff_confirmation=bool(data["offer_handoff_confirmation"]),
        )
    return OrchestratorDecision(
        type="unavailable_response",
        response=response,
        covered_goal_ids=data["covered_goal_ids"],
        control_action="finish_unavailable",
        unavailable_parts=list(data["unavailable_parts"]),
        offer_handoff_confirmation=bool(data["offer_handoff_confirmation"]),
        observation_coverage_failure=bool(data["observation_coverage_failure"]),
    )


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return "" if content is None else str(content)
