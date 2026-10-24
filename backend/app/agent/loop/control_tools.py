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

if TYPE_CHECKING:
    from app.agent.loop.query_loop import QuerySession


class CompletionAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: Literal[True] = True


class RegistrationAccepted(CompletionAccepted):
    trusted_references: dict[str, Any]
    query_state: list[dict[str, Any]]


def control_tools(session: "QuerySession") -> list:

    @tool(inputSchema={"json": RegistrationCall.model_json_schema()})
    def register_goals(registration: GoalRegistration) -> dict:
        """Register all goals, then propose independent queries in this SAME response.

        If no query is needed or possible, follow with query_complete in this SAME
        response. Each business call explicitly names its goal_id; a goal may
        have multiple calls. No activation round trip is needed. Source text is verbatim.
        """
        try:
            registration = GoalRegistration.model_validate(registration)
            references = session._register(registration, session.book)
            return RegistrationAccepted(
                trusted_references=references, **session.control_state(),
            ).model_dump(mode="json")
        except (AdmissionError, ValidationError) as exc:
            return session.reject_control(
                "register_goals", exc, registration.model_dump(mode="json")
                if isinstance(registration, GoalRegistration) else registration,
            )

    @tool(inputSchema={"json": CompletionCall.model_json_schema()})
    def query_complete(completion: QueryCompletion) -> dict:
        """Stop querying and hand every goal and actual result to the answer stage.

        Supply only a short reason, never goal statuses or evidence IDs. This does not
        claim success. Usually standalone; may follow initial registration if no queries
        will be made. Never mix with business queries in the same response.
        """
        try:
            session.book.finish(QueryCompletion.model_validate(completion))
            session.stop_reason = "completed"
            return CompletionAccepted().model_dump(mode="json")
        except (AdmissionError, ValidationError) as exc:
            return session.reject_control("query_complete", exc, completion.model_dump(mode="json")
                                       if isinstance(completion, QueryCompletion)
                                       else completion)

    return [register_goals, query_complete]
