"""Request-local evidence from actual tool executions, independent of the old DAG."""

from copy import deepcopy
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from app.tools.catalog_search.outcomes import ResultStatus
from app.tools.schemas import ToolExecutionResult


class EvidenceError(ValueError):
    """An untrusted reference cannot establish the requested claim."""


class ToolInvocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: str
    operation_id: str
    goal_id: str
    tool_name: str
    arguments: dict[str, Any]
    attempt: int
    started_at: datetime
    elapsed_seconds: float
    status: Literal["succeeded", "failed", "cancelled"]


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    invocation: ToolInvocation
    result: ToolExecutionResult
    recorded_at: datetime

    @property
    def completion_blockers(self) -> tuple[str, ...]:
        """Conservative completeness guard, NOT a semantic goal-satisfaction verdict.

        Domain status/diagnostics remain authoritative in result.output. Even an
        unrestricted record cannot establish an arbitrary natural-language condition.
        """
        if not self.result.ok or self.result.output is None:
            return ("execution_failed",)
        output = self.result.output
        blockers = []
        name = self.invocation.tool_name
        if name in {"catalog_search", "catalog_lookup"}:
            if output["status"] not in {
                ResultStatus.SUCCESS, ResultStatus.NO_MATCH, ResultStatus.NOT_FOUND,
            }:
                blockers.append(f"domain_status:{output['status']}")
        elif name == "order_lookup":
            if output["result_type"] in {
                "unsupported", "invalid_query", "planning_failed", "order_suggestions",
            }:
                blockers.append(f"domain_status:{output['result_type']}")
        elif name in {"policy_search", "knowledge_search"}:
            # Ranked retrieval supplies sources, never proof of an exhaustive corpus.
            blockers.append("retrieval_not_exhaustive")
            if not output["documents"]:
                blockers.append("no_retrieval_evidence")
        for field in ("is_exhaustive", "matching_complete"):
            if field in output and output[field] is not True:
                blockers.append(field)
        if output.get("next_offset") is not None:
            blockers.append("remaining_window")
        if output.get("field_issues") or output.get("data_quality") == "issues":
            blockers.append("field_issues")
        for diagnostic in output.get("diagnostics", []):
            if (
                diagnostic.get("severity") in {"warning", "error"}
                or diagnostic.get("recommended_action", "use_result") not in {
                    "use_result", "explain_no_match", "explain_not_found",
                }
            ):
                blockers.append(f"diagnostic:{diagnostic['code']}")
        return tuple(dict.fromkeys(blockers))

    def observation(self) -> dict[str, Any]:
        """Lossless P1 observation; bounded query projection is a P2 consumer concern."""
        return {
            **self.model_dump(mode="json"),
            "completion_blockers": list(self.completion_blockers),
        }


class EvidenceStore:
    """One instance per request. No model-write API; reads return detached copies."""

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}
        self._superseded: set[str] = set()

    def record(
        self, invocation: ToolInvocation, result: ToolExecutionResult, recorded_at: datetime,
    ) -> EvidenceRecord:
        if result.tool_name != invocation.tool_name:
            raise EvidenceError("tool_result_mismatch")
        record = EvidenceRecord(
            evidence_id=f"ev_{uuid4().hex}", invocation=invocation,
            result=result, recorded_at=recorded_at,
        ).model_copy(deep=True)
        for previous in self._records.values():
            if previous.invocation.operation_id == invocation.operation_id:
                self._superseded.add(previous.evidence_id)
        self._records[record.evidence_id] = record
        return record.model_copy(deep=True)

    def active_records(self) -> list[EvidenceRecord]:
        return [record.model_copy(deep=True) for key, record in self._records.items()
                if key not in self._superseded]

    def all_records(self) -> list[EvidenceRecord]:
        return [record.model_copy(deep=True) for record in self._records.values()]

    def reference(
        self, goal_id: str, evidence_id: str, *, require_complete: bool = False,
        require_success: bool = False,
    ) -> EvidenceRecord:
        record = self._records.get(evidence_id)
        if record is None:
            raise EvidenceError("unknown_evidence")
        if record.invocation.goal_id != goal_id:
            raise EvidenceError("wrong_goal")
        if evidence_id in self._superseded:
            raise EvidenceError("superseded_evidence")
        if require_success and not record.result.ok:
            raise EvidenceError("execution_failed")
        if require_complete and record.completion_blockers:
            raise EvidenceError("incomplete_evidence")
        return record.model_copy(deep=True)

    def supersede(self, goal_id: str, previous_id: str, replacement_id: str) -> None:
        if previous_id == replacement_id:
            raise EvidenceError("same_evidence")
        self.reference(goal_id, previous_id)
        self.reference(goal_id, replacement_id, require_success=True)
        self._superseded.add(previous_id)

    def presentation(self, goal_id: str, evidence_id: str) -> dict[str, Any]:
        """Select cards/sources by evidence ID only; retain surrounding limitations.

        This is a candidate bundle, not ChatResponse or a claim that all cards match.
        In particular suggestions remain suggestions and never become exact orders.
        """
        record = self.reference(goal_id, evidence_id, require_success=True)
        output = record.result.output or {}
        fields = {
            "catalog_search": ("products",),
            "catalog_lookup": ("products", "lookup_summary"),
            "catalog_facets": ("items",),
            "order_lookup": ("order", "candidates", "analysis_orders", "suggestions"),
            "policy_search": ("documents",),
            "knowledge_search": ("documents",),
        }[record.invocation.tool_name]
        return {
            "evidence": record.observation(),
            "candidates": deepcopy({key: output[key] for key in fields if key in output}),
        }
