"""Pure outcome policy tests: query facts determine a small, stable public status set."""

import pytest

from app.tools.catalog_search.outcomes import (
    ResultReason,
    completed_policy,
    policy_for,
    validate_policy_fields,
)


@pytest.mark.parametrize(
    "facts,status,reason",
    [
        ({"total_matches": 2, "returned_count": 1}, "success", "matches_found"),
        ({"total_matches": 2, "returned_count": 0}, "success", "matches_found"),
        (
            {"total_matches": 2, "returned_count": 1, "unknown_count": 3},
            "success",
            "matches_found",
        ),
        ({}, "no_match", "no_matching_sku"),
        ({"unknown_count": 1}, "unsupported", "data_insufficient"),
        ({"object_status": "not_found"}, "not_found", "target_not_found"),
        ({"named_target_missing": True}, "not_found", "target_not_found"),
        ({"object_status": "inactive"}, "unsupported", "target_inactive"),
        ({"object_status": "outside_scope"}, "unsupported", "target_outside_scope"),
    ],
)
def test_query_facts_select_outcome_without_extra_statuses(facts, status, reason):
    inputs = {"total_matches": 0, "returned_count": 0, "unknown_count": 0} | facts
    policy = completed_policy(**inputs)
    assert (policy.status, policy.reason) == (status, reason)
    assert policy.retryable is False


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "no_match"},
        {"action": "explain_no_match"},
        {"retryable": True},
        {"query_state": "identity_only"},
    ],
)
def test_insufficient_data_policy_cannot_claim_no_match_or_retry(changes):
    values = {
        "status": "unsupported",
        "reason": "data_insufficient",
        "action": "explain_limitation",
        "retryable": False,
        "query_state": "completed",
    }
    with pytest.raises(ValueError):
        validate_policy_fields(**(values | changes))


@pytest.mark.parametrize("phase", ["not_executed", "completed"])
def test_insufficient_data_can_be_detected_before_or_after_matching(phase):
    policy = policy_for(ResultReason.DATA_INSUFFICIENT)
    validate_policy_fields(
        status=policy.status,
        reason=policy.reason,
        action=policy.action,
        retryable=policy.retryable,
        query_state=phase,
    )


@pytest.mark.parametrize(
    "facts,reason",
    [
        ({"alternative_count": 2}, "alternatives_found"),
        ({"object_fact_count": 1, "unknown_count": 1}, "object_facts_returned"),
    ],
)
def test_partial_candidates_have_explicit_outcomes_without_strict_match_counts(facts, reason):
    inputs = {"total_matches": 0, "returned_count": 0, "unknown_count": 0} | facts
    policy = completed_policy(**inputs)
    assert policy.status == "partial" and policy.reason == reason
    assert policy.action == "explain_differences"


def test_unknown_matching_cannot_be_misclassified_as_an_alternative():
    with pytest.raises(ValueError):
        completed_policy(total_matches=0, returned_count=0, unknown_count=1, alternative_count=1)
