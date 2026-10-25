"""Offline input contracts for catalog_lookup; business cases live in cases.json."""
import pytest

from tests.tools.shared.catalog_contract_cases import (
    assert_invalid_public_arguments_stop_before_handler,
    contract_cases,
)

CASES = contract_cases("catalog_lookup")


@pytest.mark.contract
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_invalid_public_arguments_stop_before_handler(case):
    assert_invalid_public_arguments_stop_before_handler("catalog_lookup", case)
