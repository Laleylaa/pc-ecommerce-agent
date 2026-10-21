from typing import Any

import pytest
from app.agent.tool_outcomes import normalize_tool_result

from tests.support.orchestration_orchestrator_outcomes import (
    _result,
)


@pytest.mark.parametrize(
    ("result", "expected_outcome", "usable"),
    [
        (
            _result(
                "catalog_search",
                {"result_type": "products", "products": [{"sku_id": 1}]},
            ),
            "usable",
            True,
        ),
        (
            _result("catalog_search", {"result_type": "empty", "products": []}),
            "empty",
            False,
        ),
        (
            _result(
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "diagnostics": [{"code": "invalid_catalog_plan"}],
                },
            ),
            "error",
            False,
        ),
        (
            _result(
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "diagnostics": [{"code": "unsupported_query"}],
                },
            ),
            "unsupported",
            False,
        ),
        (
            _result(
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "unsupported_query",
                    "query_plan": {
                        "supported": False,
                        "error_type": "usage_mapping_unavailable",
                        "usage_mapping": {"status": "unavailable"},
                    },
                    "diagnostics": [{"code": "usage_mapping_unavailable"}],
                },
            ),
            "empty",
            False,
        ),
        (
            _result(
                "catalog_search",
                {"result_type": "empty", "products": [{"sku_id": 1}]},
            ),
            "insufficient",
            False,
        ),
        (
            _result(
                "catalog_search",
                {
                    "result_type": "empty",
                    "products": [],
                    "ranking_strategy": "unsupported_query",
                    "query_plan": {"supported": False},
                },
            ),
            "unsupported",
            False,
        ),
        (
            _result(
                "catalog_facets",
                {"result_type": "facets", "items": [{"value": "Logitech"}]},
            ),
            "usable",
            True,
        ),
        (
            _result("order_lookup", {"result_type": "not_found", "candidates": []}),
            "not_found",
            False,
        ),
        (
            _result(
                "order_lookup",
                {
                    "result_type": "unsupported",
                    "diagnostics": [{"code": "unsupported_query"}],
                    "query_plan": {
                        "supported": False,
                        "unsupported_fields": ["receiver_address"],
                    },
                },
            ),
            "unsupported",
            False,
        ),
        (
            _result(
                "order_lookup",
                {
                    "result_type": "planning_failed",
                    "diagnostics": [{"code": "invalid_order_plan"}],
                },
            ),
            "error",
            False,
        ),
        (
            _result(
                "order_lookup",
                {
                    "result_type": "single_order",
                    "order": {"id": 202607020001},
                    "diagnostics": [{"code": "partial_unsupported_query"}],
                    "query_plan": {
                        "supported": True,
                        "fully_supported": False,
                        "unsupported_fields": ["payment_method"],
                    },
                },
            ),
            "usable",
            True,
        ),
        (
            _result("knowledge_search", {"result_type": "empty", "documents": []}),
            "insufficient",
            False,
        ),
        (_result("policy_search", ok=False), "error", False),
    ],
)
def test_normalize_tool_result_distinguishes_execution_from_usable_information(
    result: dict[str, Any],
    expected_outcome: str,
    usable: bool,
) -> None:
    outcome = normalize_tool_result(result)

    assert outcome.outcome == expected_outcome
    assert outcome.has_usable_information is usable
