import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.legacy.decisions import OrchestratorDecision, infer_tool_subquery
from app.agent.tool_outcomes import (
    OutcomeType,
    ToolOutcome,
    _optional_string,
    normalize_tool_result,
)

# A stopped query is not an empty query. Shared with task readiness and answer projection.

SubqueryStatus = Literal[
    "ready_to_answer",
    "unavailable",
    "needs_replan",
    "failed",
    "superseded",
    "answered",
]




class SubqueryLedgerEntry(ToolOutcome):
    subquery: str
    subquery_id: str = ""
    canonical_query: str = ""
    query_fingerprint: str = ""
    initial_tool_call_id: str = ""
    status: SubqueryStatus
    wave: int
    arguments: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = ""
    reused_from_tool_call_id: str | None = None


class TerminalValidation(BaseModel):
    valid: bool
    reason: str = ""




def build_subquery_ledger(
    tool_waves: Sequence[Mapping[str, Any]],
) -> list[SubqueryLedgerEntry]:
    entries: dict[str, SubqueryLedgerEntry] = {}
    initial_query_by_subquery: dict[str, tuple[str, str, str]] = {}
    for wave_index, wave in enumerate(tool_waves, start=1):
        wave_number = int(wave.get("wave") or wave_index)
        calls = {
            str(call.get("id") or ""): call
            for call in wave.get("calls", [])
            if isinstance(call, Mapping)
        }
        for result in wave.get("results", []):
            if not isinstance(result, Mapping):
                continue
            outcome = normalize_tool_result(result)
            call = calls.get(outcome.tool_call_id, {})
            arguments = call.get("arguments") if isinstance(call, Mapping) else {}
            argument_mapping = arguments if isinstance(arguments, Mapping) else {}
            subquery = (
                str(call.get("subquery") or "").strip()
                or infer_tool_subquery(outcome.tool_name, dict(argument_mapping))
            )
            subquery_key = _normalized_text(subquery)
            query = _display_query(
                call.get("canonical_query") or argument_mapping.get("query")
            )
            identity = initial_query_by_subquery.setdefault(
                subquery_key,
                (
                    _subquery_id(subquery_key),
                    query,
                    outcome.tool_call_id,
                ),
            )
            fingerprint = str(call.get("fingerprint") or "")
            if not fingerprint:
                fingerprint = tool_call_fingerprint(
                    outcome.tool_name,
                    argument_mapping,
                )
            entries[outcome.tool_call_id] = SubqueryLedgerEntry(
                **outcome.model_dump(mode="json"),
                subquery=subquery,
                subquery_id=identity[0],
                canonical_query=identity[1],
                query_fingerprint=query_fingerprint(identity[1]),
                initial_tool_call_id=identity[2],
                status=_subquery_status(outcome.outcome),
                wave=wave_number,
                arguments=dict(argument_mapping),
                fingerprint=fingerprint,
                reused_from_tool_call_id=_optional_string(
                    result.get("reused_from_tool_call_id")
                ),
            )
    ledger = list(entries.values())
    latest_by_subquery: dict[str, int] = {}
    for index, entry in enumerate(ledger):
        key = entry.subquery.strip().casefold()
        previous_index = latest_by_subquery.get(key)
        if previous_index is not None:
            ledger[previous_index].status = "superseded"
        latest_by_subquery[key] = index
    return ledger


def _subquery_status(outcome: OutcomeType) -> SubqueryStatus:
    if outcome == "usable":
        return "ready_to_answer"
    if outcome in {"empty", "not_found", "unsupported"}:
        return "unavailable"
    if outcome == "insufficient":
        return "needs_replan"
    return "failed"


def tool_call_fingerprint(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Return a stable identity for an executed tool and its effective arguments."""
    payload = {
        "tool_name": tool_name.strip().casefold(),
        "arguments": _canonicalize(arguments),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def query_fingerprint(query: Any) -> str:
    return hashlib.sha256(_normalized_text(query).encode("utf-8")).hexdigest()


def _display_query(query: Any) -> str:
    return " ".join(str(query or "").split())


def _normalized_text(value: Any) -> str:
    return _display_query(value).casefold()


def _subquery_id(normalized_subquery: str) -> str:
    digest = hashlib.sha256(normalized_subquery.encode("utf-8")).hexdigest()[:12]
    return f"sq_{digest}"


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not _is_empty_argument(item)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized = [_canonicalize(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _is_empty_argument(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def has_usable_information(ledger: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        is_active_ledger_entry(entry) and bool(entry.get("has_usable_information"))
        for entry in ledger
    )


def is_active_ledger_entry(entry: Mapping[str, Any]) -> bool:
    """Return whether an observation may still support the current answer."""
    return entry.get("status") != "superseded"


def active_usable_tool_call_ids(ledger: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(entry.get("tool_call_id"))
        for entry in ledger
        if is_active_ledger_entry(entry)
        and entry.get("has_usable_information")
        and entry.get("tool_call_id")
    ]


def validate_terminal_decision(
    decision: OrchestratorDecision,
    ledger: Sequence[Mapping[str, Any]],
    *,
    planned_subquery_ids: Sequence[str] = (),
    clarification_allowed: bool = False,
    resolved_task_ids: Sequence[str] | None = None,
    usable_artifact_tool_call_ids: Sequence[str] | None = None,
    answerable_tool_call_ids: Sequence[str] | None = None,
    supporting_tool_call_ids: Sequence[str] = (),
    boundary_consistent: bool = True,
    handoff_confirmation_allowed: bool = False,
    observation_coverage_failure_allowed: bool = False,
) -> TerminalValidation:
    action = decision.control_action
    if not decision.response.strip() and decision.type != "invalid":
        return TerminalValidation(valid=False, reason="blank_answer")
    if decision.type == "invalid" or action is None:
        return TerminalValidation(valid=False, reason="missing_or_invalid_control_action")
    if not boundary_consistent:
        return TerminalValidation(valid=False, reason="terminal_boundary_changed")
    if decision.offer_handoff_confirmation and (
        not handoff_confirmation_allowed
        or action not in {"finish_partial", "finish_unavailable"}
    ):
        return TerminalValidation(
            valid=False,
            reason="handoff_confirmation_not_allowed_for_terminal_state",
        )

    active_entries = [entry for entry in ledger if is_active_ledger_entry(entry)]
    usable_ids = (
        {
            str(call_id)
            for call_id in usable_artifact_tool_call_ids
            if str(call_id)
        }
        if usable_artifact_tool_call_ids is not None
        else set(active_usable_tool_call_ids(ledger))
    )
    active_ids = {str(entry.get("tool_call_id")) for entry in active_entries}
    answerable_ids = (
        {str(call_id) for call_id in answerable_tool_call_ids if str(call_id)}
        if answerable_tool_call_ids is not None
        else usable_ids
    )
    used_ids = set(decision.used_tool_call_ids)

    initial_subqueries = {
        str(subquery).strip().casefold()
        for subquery in planned_subquery_ids
        if str(subquery).strip()
    } or {
        str(entry.get("subquery") or "").strip().casefold()
        for entry in ledger
        if entry.get("wave") == 1 and str(entry.get("subquery") or "").strip()
    }
    resolved_subqueries = (
        {
            str(task_id).strip().casefold()
            for task_id in resolved_task_ids
            if str(task_id).strip()
        }
        if resolved_task_ids is not None
        else {
            str(entry.get("subquery") or "").strip().casefold()
            for entry in active_entries
            if entry.get("has_usable_information")
            and str(entry.get("subquery") or "").strip()
        }
    )
    all_initial_subqueries_resolved = initial_subqueries <= resolved_subqueries

    if action == "ask_clarification":
        if answerable_ids:
            return TerminalValidation(
                valid=False,
                reason="clarification_cannot_discard_answerable_task_results",
            )
        return _terminal_validation(
            decision.type == "clarification" and clarification_allowed,
            "clarification_requires_user_suppliable_missing_artifact",
        )
    if action == "finish_answer":
        valid = (
            bool(used_ids)
            and used_ids == answerable_ids
            and all_initial_subqueries_resolved
        )
        return _terminal_validation(
            decision.type == "grounded_response" and valid,
            (
                "finish_answer_requires_all_initial_subqueries_resolved"
                if not all_initial_subqueries_resolved
                else "finish_answer_requires_only_answerable_tool_call_ids"
            ),
        )
    if action == "finish_partial":
        partial_ids = answerable_ids | (set(supporting_tool_call_ids) & active_ids & usable_ids)
        valid = (
            bool(used_ids)
            and used_ids <= partial_ids
            and bool(decision.unavailable_parts)
        )
        return _terminal_validation(
            decision.type == "partial_response" and valid,
            "finish_partial_requires_answerable_ids_and_unavailable_parts",
        )
    if action == "finish_unavailable":
        coverage_failure = (
            decision.observation_coverage_failure
            and observation_coverage_failure_allowed
            and not used_ids
        )
        valid = (
            bool(active_ids)
            and bool(decision.unavailable_parts)
            and (not answerable_ids or coverage_failure)
        )
        return _terminal_validation(
            decision.type == "unavailable_response" and valid,
            (
                "finish_unavailable_requires_no_answerable_task_or_validated_coverage_failure"
            ),
        )
    return TerminalValidation(valid=False, reason="unknown_control_action")














def _terminal_validation(valid: bool, invalid_reason: str) -> TerminalValidation:
    return TerminalValidation(valid=valid, reason="" if valid else invalid_reason)
