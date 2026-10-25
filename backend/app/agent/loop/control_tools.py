"""Session-bound control definitions; schemas and descriptions have one owner."""

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from strands import tool

from app.agent.loop.query_schema import (
    CompletionCall,
    GoalRegistration,
    QueryCompletion,
    RegistrationCall,
)
from app.agent.loop.tool_adapter import AdmissionError
from app.tools.output_limits import DEFAULT_OUTPUT_TOKEN_BUDGET, output_cost

if TYPE_CHECKING:
    from app.agent.loop.query_loop import QuerySession


class CompletionAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: Literal[True] = True


class RegistrationAccepted(CompletionAccepted):
    trusted_references: dict[str, Any]
    query_state: list[dict[str, Any]]


def bounded_control_result(result: dict) -> dict:
    """Keep accepted-state truthful when a control response is too large."""
    if output_cost(result) <= DEFAULT_OUTPUT_TOKEN_BUDGET:
        return result
    return {
        "accepted": bool(result.get("accepted", False)),
        "output_budget_exceeded": True,
        "message": "控制结果超过预算，详细状态未展示；不要重放已接受操作或猜测引用编号。",
        "token_limit": DEFAULT_OUTPUT_TOKEN_BUDGET,
        "counting_method": "utf8_byte_upper_bound",
    }


def control_tools(session: "QuerySession") -> list:

    @tool(inputSchema={"json": RegistrationCall.model_json_schema()})
    def register_goals(registration: GoalRegistration) -> dict:
        """Register all goals, then propose independent queries in this SAME response.

        If no query is needed or possible, follow with query_complete in this SAME
        response. Each business call explicitly names its goal_id; a goal may
        have multiple calls. No activation round trip is needed. Source text is verbatim.
        Output is budgeted; oversize state is explicitly marked with token_limit.
        Never invent omitted references or replay accepted registrations.
        """
        try:
            registration = GoalRegistration.model_validate(registration)
            references = session._register(registration, session.book)
            return bounded_control_result(RegistrationAccepted(
                trusted_references=references, **session.control_state(),
            ).model_dump(mode="json"))
        except (AdmissionError, ValidationError) as exc:
            return bounded_control_result(session.reject_control(
                "register_goals", exc, registration.model_dump(mode="json")
                if isinstance(registration, GoalRegistration) else registration,
            ))

    @tool(inputSchema={"json": CompletionCall.model_json_schema()})
    def query_complete(completion: QueryCompletion) -> dict:
        """Stop querying and hand every goal and actual result to the answer stage.

        Supply only a short reason, never goal statuses or evidence IDs. This does not
        claim success. Usually standalone; may follow initial registration if no queries
        will be made. Never mix with business queries in the same response.
        Output is budgeted; an oversized error is explicitly summarized.
        """
        try:
            session.book.finish(QueryCompletion.model_validate(completion))
            session.stop_reason = "completed"
            return CompletionAccepted().model_dump(mode="json")
        except (AdmissionError, ValidationError) as exc:
            return bounded_control_result(session.reject_control(
                "query_complete", exc, completion.model_dump(mode="json")
                if isinstance(completion, QueryCompletion) else completion,
            ))

    return [register_goals, query_complete]
