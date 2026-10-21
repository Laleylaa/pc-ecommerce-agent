from datetime import datetime

import pytest

from tests.support.orders_order_suggestions import lookup as lookup
from tests.support.orders_order_suggestions import (
    september,
)


@pytest.mark.asyncio
async def test_exact_product_and_month_result_needs_no_confirmation(lookup):
    run, calls, _ = lookup
    result = await run(september())
    assert result.result_type == "order_analysis"
    assert result.focus_order_ids == [102]
    assert not result.suggestions and not result.diagnostics
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["all", "latest"])
async def test_misremembered_month_preserves_product_and_status(lookup, selection):
    run, calls, _ = lookup
    result = await run(september(statuses=[4]), selection=selection)
    assert result.result_type == "order_suggestions"
    assert result.total_match_count == result.returned_count == 0
    assert result.focus_order_ids == [] and result.analysis_orders == []
    assert [s.order.id for s in result.suggestions] == [101]
    difference = result.suggestions[0].differences[0]
    assert difference.field == "time_range" and difference.state == "not_matched"
    assert difference.actual == "2026-08-15T00:00:00"
    assert result.suggestion_filters.statuses == [4]
    assert all(user == 7 and f.product_terms == ["K87"] for user, f in calls)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_misremembered_status_preserves_product_and_month(lookup):
    run, calls, _ = lookup
    result = await run(september(statuses=[2]))
    assert result.result_type == "order_suggestions"
    assert [s.order.id for s in result.suggestions] == [102]  # excludes user 8 and K98
    difference = result.suggestions[0].differences[0]
    assert difference.field == "statuses" and difference.expected == {"statuses": [2]}
    assert difference.actual == 3 and difference.state == "not_matched"
    assert result.suggestion_filters.start_at == datetime(2026, 9, 1)
    assert len(calls) == 3  # strict, time-only relaxation empty, status-only relaxation


@pytest.mark.asyncio
async def test_missing_payment_timestamp_is_unknown_not_wrong_date(lookup):
    run, _, _ = lookup
    result = await run(september(time_field="pay_at"))
    assert result.result_type == "order_suggestions"
    assert all(s.differences[0].state == "unknown" and s.differences[0].actual is None
               for s in result.suggestions)


@pytest.mark.asyncio
async def test_suggestion_window_does_not_change_exact_count(lookup):
    run, _, _ = lookup
    result = await run(september(time_field="pay_at"), limit=1)
    assert result.suggestion_total_count == 2
    assert len(result.suggestions) == 1 and not result.suggestion_is_exhaustive
    assert result.total_match_count == 0 and result.is_exhaustive


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["count", "unsupported", "explicit", "wrong_model", "no_product"])
async def test_no_silent_relaxation_of_unsupported_or_identity_requirements(lookup, case):
    run, calls, _ = lookup
    options = {}
    filters = september(statuses=[2])
    if case == "count":
        options["operation"] = "total_count"
    elif case == "unsupported":
        options["unresolved"] = ["周五前一定送到"]
    elif case == "explicit":
        options.update(order_id=999, selection="explicit")
    elif case == "wrong_model":
        filters = filters.model_copy(update={"product_terms": ["K999"]})
    else:
        options["operation"] = "list"
        filters = filters.model_copy(update={"product_terms": [], "statuses": [2]})
    result = await run(filters, **options)
    assert not result.suggestions
    assert result.result_type in {"not_found", "order_count", "unsupported"}
    assert len(calls) <= (3 if case == "wrong_model" else 1)
