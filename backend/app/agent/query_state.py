"""Program-owned goals, execution records and query lifecycle, not semantic verdicts."""

from typing import Literal

from app.agent.evidence import EvidenceRecord, EvidenceStore
from app.agent.query_schema import GoalRegistration, QueryCompletion, QueryGoal
from app.agent.tool_adapter import AdmissionError


def evidence_progress(record: EvidenceRecord) -> Literal["handled", "blocked", "failed"]:
    """Classify explicit tool result fields for declared prerequisite checks only."""
    if not record.result.ok:
        return "failed"
    output = record.result.output or {}
    status = output.get("status", output.get("result_type"))
    if status in {"planner_error", "split_required", "planning_failed"}:
        return "failed"
    if status in {"clarification_required", "invalid_query", "unsupported", "order_suggestions"}:
        return "blocked"
    if record.invocation.tool_name in {"policy_search", "knowledge_search"}:
        return "handled" if output.get("documents") else "blocked"
    if any(
        d.get("recommended_action")
        not in {
            None,
            "use_result",
            "explain_no_match",
            "explain_not_found",
            "explain_differences",
        }
        for d in output.get("diagnostics", [])
    ):
        return "blocked"
    return "handled"


class GoalBook:
    def __init__(self, message: str, evidence: EvidenceStore):
        self.message, self.evidence = message, evidence
        self.goals: dict[str, QueryGoal] = {}
        self.completion: QueryCompletion | None = None

    def register(self, registration: GoalRegistration) -> None:
        if self.completion is not None:
            raise AdmissionError("query_already_complete")
        incoming = {goal.id: goal for goal in registration.goals}
        if len(incoming) != len(registration.goals):
            raise AdmissionError("duplicate_goal")
        if not self.goals.keys() <= incoming.keys():
            raise AdmissionError("cannot_remove_goals")
        for goal in incoming.values():
            if not goal.source_text.strip() or goal.source_text not in self.message:
                raise AdmissionError("goal_source_not_in_current_request")
            if goal.id in self.goals and goal != self.goals[goal.id]:
                raise AdmissionError("cannot_rewrite_registered_goal")
            for prerequisite in goal.prerequisites:
                if prerequisite.goal_id not in incoming or prerequisite.goal_id == goal.id:
                    raise AdmissionError("invalid_prerequisite")
                if prerequisite.source_text not in self.message:
                    raise AdmissionError("prerequisite_source_not_in_request")

        def visit(goal_id: str, path: set[str]) -> None:
            if goal_id in path:
                raise AdmissionError("cyclic_prerequisite")
            for prerequisite in incoming[goal_id].prerequisites:
                visit(prerequisite.goal_id, path | {goal_id})

        for goal_id in incoming:
            visit(goal_id, set())
        self.goals = {key: value.model_copy(deep=True) for key, value in incoming.items()}

    def records(self, goal_id: str) -> list[EvidenceRecord]:
        return [r for r in self.evidence.active_records() if r.invocation.goal_id == goal_id]

    def prerequisites_met(self, goal: QueryGoal) -> bool:
        for prerequisite in goal.prerequisites:
            records = self.records(prerequisite.goal_id)
            # Unstructured policy/eligibility conditions have no trustworthy automatic predicate.
            # Keep them blocked instead of treating a model assertion as authorization.
            if prerequisite.requirement == "condition":
                return False
            found = False
            for record in records:
                if not record.result.ok or evidence_progress(record) != "handled":
                    continue
                output = record.result.output or {}
                if prerequisite.requirement == "product_found":
                    found |= bool(output.get("products")) and output.get("match_kind") == "exact"
                elif prerequisite.requirement == "order_found":
                    found |= (
                        bool(output.get("order")) and output.get("result_type") == "single_order"
                    )
                elif prerequisite.requirement == "sources_found":
                    found |= bool(output.get("documents"))
            if not found:
                return False
        return True

    def admit(self, goal_id: str) -> None:
        if self.completion is not None:
            raise AdmissionError("query_already_complete")
        goal = self.goals.get(goal_id)
        if goal is None or goal.disposition != "tool_planning":
            raise AdmissionError("goal_not_admitted")
        if not self.prerequisites_met(goal):
            raise AdmissionError("prerequisite_unproven")

    def execution(self, goal: QueryGoal) -> dict[str, int]:
        """Count actual attempts, including failed attempts superseded by retries.

        ok is the tool envelope flag, not proof that the user's request is satisfied.
        Domain outcomes (e.g. split_required/no_match) remain on each result.
        """
        attempts = [
            record for record in self.evidence.all_records()
            if record.invocation.goal_id == goal.id
        ]
        return {
            "attempt_count": len(attempts),
            "ok_count": sum(record.result.ok for record in attempts),
            "error_count": sum(not record.result.ok for record in attempts),
            "active_result_count": len(self.records(goal.id)),
        }

    def finish(self, proposed: QueryCompletion) -> None:
        if self.completion is not None:
            raise AdmissionError("query_already_complete")
        if not self.goals:
            raise AdmissionError("goals_not_registered")
        # Stopping is permitted with missing/failed results. It asserts no goal success;
        # outcome() and handoff() always collect the real goals and records themselves.
        self.completion = proposed.model_copy(deep=True)

    def outcome(self, stop_reason: str) -> list[dict]:
        return [
            {
                **goal.model_dump(mode="json"),
                "execution": self.execution(goal),
                "evidence_ids": [record.evidence_id for record in self.records(goal.id)],
                # Completion prose is not factual evidence; use program state.
                "stop_reason": stop_reason,
                "prerequisites_met": self.prerequisites_met(goal),
            }
            for goal in self.goals.values()
        ]
